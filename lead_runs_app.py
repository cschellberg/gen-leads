"""
Standalone app for tracking LinkedIn scrape coverage and running the lead
enrichment pipeline.

This app does NOT fetch anything from LinkedIn itself -- see linkedin_import.py
for why. The actual scraping happens in a session with Claude (which drives
a real browser and calls linkedin_import.ingest_linkedin_page_text() for
each page), which is what populates `leads` (as unprocessed) and logs each
run into `lead_runs`. This app's "Log Run" button is for recording a run's
coverage manually if you ever need to; the history list below it shows
everything logged so far -- the answer to "which pages have I already
covered?".

"Process" runs lead_gen.py's enrichment pass (website + contact search,
ranking, drafted email) on every unprocessed lead currently in the
database. It can take a while and makes real Gemini API calls (web-search-
grounded lookups plus drafting), so it runs on a background thread with
live progress shown below; an optional Limit lets you test on just a few
first.

Run:
    python lead_runs_app.py
"""

import os
import queue
import sys
import threading
from pathlib import Path

# On some Windows venvs, tkinter's default Tcl/Tk search path doesn't match
# the python.org installer layout (<base>/tcl/tcl8.6, not <base>/lib/tcl8.6),
# so Tk() fails with "Can't find a usable init.tcl". Point it at the right
# place -- before tkinter is touched -- if it looks like that's needed.
if sys.platform == "win32":
    _base = Path(getattr(sys, "base_prefix", sys.prefix))
    for _var, _dirname in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        if _var not in os.environ:
            _candidate = _base / "tcl" / _dirname
            if _candidate.is_dir():
                os.environ[_var] = str(_candidate)

import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DEFAULT_DB, Lead, LeadRun, get_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

HISTORY_URL_FIELD_WIDTH = 60  # characters -- keeps the row's Copy button on-screen


class _QueueWriter:
    """File-like object that buffers writes into complete lines and puts each
    one on a queue -- used to redirect stdout/stderr from the worker thread
    into the status box, since print() output has nowhere else to go once
    it's running inside the GUI process.
    """

    def __init__(self, q: "queue.Queue[str]"):
        self.queue = q
        self.buffer = ""

    def write(self, s: str):
        self.buffer += s
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                self.queue.put(line)

    def flush(self):
        pass


DEFAULT_SEARCH_URL = (
    "https://www.linkedin.com/search/results/companies/?keywords=NOT%20Staffing%20NOT%20Recruiting"
    "%20NOT%20Education&origin=GLOBAL_SEARCH_HEADER&companyHqGeo=%5B%22104937023%22%5D&companySize="
    "%5B%22B%22%2C%22C%22%2C%22D%22%5D"
)


class LeadRunsApp:
    def __init__(self, root: tk.Misc, active_profile_id: int | None = None):
        self.root = root
        # root is a real window (standalone run) or a plain Frame embedded
        # in main_app.py's content area (normal flow, one consolidated
        # window) -- only a real window has .title()/.geometry() to set.
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("Lead Runs")
            self._center_window(640, 640)

        self.engine = get_engine(DEFAULT_DB)
        self.process_queue: "queue.Queue[str]" = queue.Queue()
        self.processing = False
        # Which Profile newly-scraped leads get tagged with (embedded into
        # the Copy Scrape Prompt text) -- set from main_app.py's Profile
        # dropdown; None means "let ingest fall back to auto-assignment".
        self.active_profile_id = active_profile_id

        self._build_top_bar()
        self._build_log_run_section()
        self._build_history_section()
        self._build_process_section()
        self.refresh_all()

    def set_active_profile_id(self, profile_id: int | None) -> None:
        """Called by main_app.py when the Profile dropdown changes while
        this window is already open, so a later Copy Scrape Prompt uses the
        newly-selected profile instead of a stale one."""
        self.active_profile_id = profile_id

        self.refresh_all()

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=12, pady=(12, 0))
        ttk.Button(bar, text="Refresh", command=self.refresh_all).pack(side="right")

    def refresh_all(self):
        """Reloads everything from the database -- run history and the
        unprocessed-lead count. Use this after Claude runs a scrape (or logs
        a run) directly against the database outside of this app, so what's
        on screen catches up.
        """
        self.refresh_history()
        self.refresh_counts()

    # ---------- Log Run ----------

    def _build_log_run_section(self):
        frame = ttk.LabelFrame(self.root, text="Log a scrape run")
        frame.pack(fill="x", padx=12, pady=(12, 6))

        row1 = ttk.Frame(frame)
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row1, text="URL:").pack(side="left")
        self.url_var = tk.StringVar(value=DEFAULT_SEARCH_URL)
        ttk.Entry(row1, textvariable=self.url_var, width=70).pack(side="left", padx=(6, 0), fill="x", expand=True)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row2, text="From Page:").pack(side="left")
        self.from_page_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.from_page_var, width=6).pack(side="left", padx=(6, 18))
        ttk.Label(row2, text="To Page:").pack(side="left")
        self.to_page_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.to_page_var, width=6).pack(side="left", padx=(6, 18))
        ttk.Button(row2, text="Copy Scrape Prompt", command=self.on_copy_scrape_prompt).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Log Run", command=self.on_log_run).pack(side="left", padx=(6, 0))

    def _read_run_fields(self):
        """Validates URL/From Page/To Page, shared by Log Run and Copy Scrape
        Prompt. Returns (url, from_page, to_page) or None (and has already
        shown an error dialog) if invalid.
        """
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Enter the search URL for this run.")
            return None
        try:
            from_page = int(self.from_page_var.get().strip())
            to_page = int(self.to_page_var.get().strip())
            if from_page < 1 or to_page < from_page:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid pages", "From Page and To Page must be whole numbers, with From ≤ To.")
            return None
        return url, from_page, to_page

    def on_copy_scrape_prompt(self):
        """Builds the same kind of prompt as scrape_prompt.txt from the
        current fields and puts it on the clipboard -- paste it into a
        Claude Code session to actually run the scrape. This app does not,
        and will not, fetch LinkedIn pages itself: see linkedin_import.py
        for why (ToS / account-ban risk of unattended automated scraping).
        Keeping a human in the loop pasting + sending the prompt each time
        is the point, not friction to route around.
        """
        fields = self._read_run_fields()
        if fields is None:
            return
        url, from_page, to_page = fields

        if self.active_profile_id is not None:
            profile_clause = (
                f"and profile_id={self.active_profile_id} "
                f"(so these new leads are tagged with that Profile's identity)"
            )
        else:
            profile_clause = (
                "and profile_id left unset (it'll auto-assign to the sole Profile in the "
                "database if there's exactly one, or need assigning manually in the Profile "
                "app otherwise)"
            )

        prompt = (
            f"Scrape pages {from_page} to {to_page} of this LinkedIn company search:\n\n"
            f"{url}\n\n"
            f"For each page in that range (append &page=N to the URL), fetch the page's\n"
            f"text via the browser tool. Concatenate all the pages' text together in\n"
            f"order, then call linkedin_import.ingest_linkedin_page_text() in gen-leads/\n"
            f"ONCE on the combined text, with from_page={from_page} and to_page={to_page},\n"
            f"url=the search URL above, {profile_clause} -- this parses every company across\n"
            f"all the pages, adds any new ones to the leads table as unprocessed, and logs\n"
            f"exactly one Lead_Runs row for the whole {from_page}-{to_page} range (calling it\n"
            f"once per page instead would log separate single-page rows rather than one range)."
        )

        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.root.update()  # keep the clipboard contents after the app loses focus
        messagebox.showinfo(
            "Copied", "Scrape prompt copied to the clipboard. Paste it into a Claude Code session to run it."
        )

    def on_log_run(self):
        fields = self._read_run_fields()
        if fields is None:
            return
        url, from_page, to_page = fields

        with Session(self.engine) as session:
            session.add(LeadRun(url=url, from_page=from_page, to_page=to_page))
            session.commit()

        self.from_page_var.set("")
        self.to_page_var.set("")
        self.refresh_history()

    def on_copy_url(self, url: str):
        """Copies one run's URL to the clipboard and also loads it into the
        URL field up in "Log a scrape run", so a past run can be quickly
        re-scraped (e.g. the next page range of the same search) without
        retyping it.
        """
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.root.update()  # keep the clipboard contents after the app loses focus
        self.url_var.set(url)

    # ---------- History ----------

    def _build_history_section(self):
        frame = ttk.LabelFrame(self.root, text="Run history (what's already been covered)")
        frame.pack(fill="both", expand=True, padx=12, pady=6)

        header = ttk.Frame(frame)
        header.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(header, text="Date", width=17, anchor="w").pack(side="left")
        ttk.Label(header, text="Pages", width=9, anchor="w").pack(side="left")
        ttk.Label(header, text="URL", width=HISTORY_URL_FIELD_WIDTH, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(header, text="", width=8).pack(side="right")

        outer = ttk.Frame(frame)
        outer.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.history_canvas = tk.Canvas(outer, height=220, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.history_canvas.yview)
        self.history_canvas.configure(yscrollcommand=scrollbar.set)
        self.history_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # One real widget per row (rather than a Treeview) so each run gets
        # its own Copy button -- a Treeview can't embed a widget per row.
        self.history_rows_frame = ttk.Frame(self.history_canvas)
        self._history_canvas_window = self.history_canvas.create_window(
            (0, 0), window=self.history_rows_frame, anchor="nw"
        )
        self.history_rows_frame.bind(
            "<Configure>",
            lambda e: self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all")),
        )
        self.history_canvas.bind(
            "<Configure>",
            lambda e: self.history_canvas.itemconfigure(self._history_canvas_window, width=e.width),
        )

    def refresh_history(self):
        for child in self.history_rows_frame.winfo_children():
            child.destroy()

        with Session(self.engine) as session:
            query = session.query(LeadRun)
            if self.active_profile_id is not None:
                query = query.filter(LeadRun.profile_id == self.active_profile_id)
            runs = query.order_by(LeadRun.id.desc()).all()
            for run in runs:
                date_str = run.run_date.strftime("%Y-%m-%d %H:%M") if run.run_date else ""
                self._add_history_row(date_str, f"{run.from_page}-{run.to_page}", run.url)

    def _add_history_row(self, date_str: str, pages_str: str, url: str):
        row = ttk.Frame(self.history_rows_frame)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=date_str, width=17, anchor="w").pack(side="left")
        ttk.Label(row, text=pages_str, width=9, anchor="w").pack(side="left")
        # Button packed (from the right) before the URL label so it always
        # claims its own space -- otherwise a long, unbounded URL string
        # pushes it out past the visible/scrollable canvas width.
        ttk.Button(row, text="Copy", width=8, command=lambda u=url: self.on_copy_url(u)).pack(side="right")
        # A read-only Entry (not a Label) so the field itself is narrow --
        # the full url is still the widget's actual content, just not all
        # visible at once (scrollable with the arrow keys/mouse, same as
        # any Entry) -- unlike a Label, its width doesn't grow to fit the
        # text, which is what kept pushing the Copy button off-screen.
        url_field = ttk.Entry(row, width=HISTORY_URL_FIELD_WIDTH)
        url_field.insert(0, url)
        url_field.configure(state="readonly")
        url_field.pack(side="left", fill="x", expand=True)

    # ---------- Process ----------

    def _build_process_section(self):
        frame = ttk.LabelFrame(self.root, text="Process unprocessed leads")
        frame.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=(8, 4))
        self.unprocessed_label = ttk.Label(row, text="")
        self.unprocessed_label.pack(side="left")

        ttk.Label(row, text="Limit (optional):").pack(side="left", padx=(18, 0))
        self.limit_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.limit_var, width=6).pack(side="left", padx=(6, 18))
        self.process_btn = ttk.Button(row, text="Process", command=self.on_process)
        self.process_btn.pack(side="left")

        self.status_text = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.status_text.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    def refresh_counts(self):
        with Session(self.engine) as session:
            count = session.query(Lead).filter(Lead.processed.is_(False)).count()
        self.unprocessed_label.config(text=f"{count} unprocessed lead(s)")

    def _append_status(self, line: str):
        self.status_text.config(state="normal")
        self.status_text.insert("end", line + "\n")
        self.status_text.see("end")
        self.status_text.config(state="disabled")

    def on_process(self):
        if self.processing:
            return
        limit_raw = self.limit_var.get().strip()
        limit = None
        if limit_raw:
            try:
                limit = int(limit_raw)
                if limit < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid limit", "Limit must be a positive whole number, or left blank.")
                return

        # Imported here, not at module load -- this makes real Gemini API
        # calls at import time (key checks), which shouldn't happen just
        # from opening the app, only when Process is actually clicked.
        try:
            from lead_gen import make_genai_client, make_llm, process_unprocessed_leads
        except SystemExit as e:
            messagebox.showerror("Configuration error", str(e))
            return

        self.processing = True
        self.process_btn.config(state="disabled")
        self.status_text.config(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.config(state="disabled")
        self._append_status("Starting...")

        threading.Thread(
            target=self._process_worker, args=(limit, make_genai_client, make_llm, process_unprocessed_leads), daemon=True
        ).start()
        self.root.after(100, self._poll_process_queue)

    def _process_worker(self, limit, make_genai_client, make_llm, process_unprocessed_leads):
        def on_progress(i, total, lead):
            self.process_queue.put(f"[{i}/{total}] {lead.name}")

        # process_company() and friends in lead_gen.py print per-lead detail
        # (website/contact/email status, failures) straight to stdout/stderr
        # rather than going through on_progress. Redirect both here so that
        # output ends up in the status box instead of a console the GUI
        # process may not even have attached.
        redirected = _QueueWriter(self.process_queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = redirected, redirected
        try:
            client = make_genai_client()
            write_llm = make_llm(temperature=0.6)
            with Session(self.engine) as session:
                done = process_unprocessed_leads(
                    session, client, write_llm, limit=limit, sleep_seconds=1.0, on_progress=on_progress
                )
            self.process_queue.put(f"__DONE__:{done}")
        except BaseException as e:
            # BaseException, not Exception -- make_llm() raises SystemExit
            # (via sys.exit()) when an API key is missing/placeholder, which
            # Exception doesn't catch. Left uncaught, the thread would die
            # silently with nothing ever posted to the queue, leaving the
            # status box stuck on "Starting..." forever with no error shown.
            self.process_queue.put(f"__ERROR__:{e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_process_queue(self):
        try:
            while True:
                msg = self.process_queue.get_nowait()
                if msg.startswith("__DONE__:"):
                    done = msg.split(":", 1)[1]
                    self._append_status(f"Done. Processed {done} lead(s).")
                    self.processing = False
                    self.process_btn.config(state="normal")
                    self.refresh_counts()
                    return
                elif msg.startswith("__ERROR__:"):
                    self._append_status(f"Error: {msg.split(':', 1)[1]}")
                    self.processing = False
                    self.process_btn.config(state="normal")
                    self.refresh_counts()
                    return
                else:
                    self._append_status(msg)
        except queue.Empty:
            pass
        if self.processing:
            self.root.after(100, self._poll_process_queue)


def main():
    root = tk.Tk()
    LeadRunsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
