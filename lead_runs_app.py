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
database. It can take a while and makes real Tavily/Gemini API calls, so it
runs on a background thread with live progress shown below; an optional
Limit lets you test on just a few first.

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

DEFAULT_SEARCH_URL = (
    "https://www.linkedin.com/search/results/companies/?keywords=NOT%20Staffing%20NOT%20Recruiting"
    "%20NOT%20Education&origin=GLOBAL_SEARCH_HEADER&companyHqGeo=%5B%22104937023%22%5D&companySize="
    "%5B%22B%22%2C%22C%22%2C%22D%22%5D"
)


class LeadRunsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lead Runs")
        self._center_window(640, 640)

        self.engine = get_engine(DEFAULT_DB)
        self.process_queue: "queue.Queue[str]" = queue.Queue()
        self.processing = False

        self._build_top_bar()
        self._build_log_run_section()
        self._build_history_section()
        self._build_process_section()

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

        prompt = (
            f"Scrape pages {from_page} to {to_page} of this LinkedIn company search:\n\n"
            f"{url}\n\n"
            f"For each page in that range (append &page=N to the URL), fetch the page's\n"
            f"text via the browser tool. Concatenate all the pages' text together in\n"
            f"order, then call linkedin_import.ingest_linkedin_page_text() in gen-leads/\n"
            f"ONCE on the combined text, with from_page={from_page} and to_page={to_page} and\n"
            f"url=the search URL above -- this parses every company across all the pages,\n"
            f"adds any new ones to the leads table as unprocessed, and logs exactly one\n"
            f"Lead_Runs row for the whole {from_page}-{to_page} range (calling it once per\n"
            f"page instead would log separate single-page rows rather than one range)."
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

    # ---------- History ----------

    def _build_history_section(self):
        frame = ttk.LabelFrame(self.root, text="Run history (what's already been covered)")
        frame.pack(fill="both", expand=True, padx=12, pady=6)

        columns = ("date", "pages", "url")
        self.history_tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        self.history_tree.heading("date", text="Date")
        self.history_tree.heading("pages", text="Pages")
        self.history_tree.heading("url", text="URL")
        self.history_tree.column("date", width=140, anchor="center")
        self.history_tree.column("pages", width=80, anchor="center")
        self.history_tree.column("url", width=380, anchor="w")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)

    def refresh_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        with Session(self.engine) as session:
            runs = session.query(LeadRun).order_by(LeadRun.id.desc()).all()
            for run in runs:
                date_str = run.run_date.strftime("%Y-%m-%d %H:%M") if run.run_date else ""
                self.history_tree.insert(
                    "", "end", values=(date_str, f"{run.from_page}-{run.to_page}", run.url)
                )

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

        # Imported here, not at module load -- this makes real Tavily/Gemini
        # API calls at import time (key checks), which shouldn't happen just
        # from opening the app, only when Process is actually clicked.
        try:
            from lead_gen import make_llm, process_unprocessed_leads
        except SystemExit as e:
            messagebox.showerror("Configuration error", str(e))
            return

        if not os.environ.get("TAVILY_API_KEY"):
            messagebox.showerror(
                "Configuration error", "TAVILY_API_KEY is not set in .env. Get a free key at https://app.tavily.com."
            )
            return

        self.processing = True
        self.process_btn.config(state="disabled")
        self.status_text.config(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.config(state="disabled")
        self._append_status("Starting...")

        threading.Thread(target=self._process_worker, args=(limit, make_llm, process_unprocessed_leads), daemon=True).start()
        self.root.after(100, self._poll_process_queue)

    def _process_worker(self, limit, make_llm, process_unprocessed_leads):
        from langchain_tavily import TavilySearch

        def on_progress(i, total, lead):
            self.process_queue.put(f"[{i}/{total}] {lead.name}")

        try:
            search = TavilySearch(max_results=5, search_depth="basic")
            extract_llm = make_llm(temperature=0)
            write_llm = make_llm(temperature=0.6)
            with Session(self.engine) as session:
                done = process_unprocessed_leads(
                    session, search, extract_llm, write_llm, limit=limit, sleep_seconds=1.0, on_progress=on_progress
                )
            self.process_queue.put(f"__DONE__:{done}")
        except Exception as e:
            self.process_queue.put(f"__ERROR__:{e}")

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
