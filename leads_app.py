"""
Standalone desktop app for browsing and acting on the leads database.

- Lists all rows from the "leads" table (db.py), 10 at a time in a scrollable
  view. Sort by natural (insertion) order or by ranking, high to low.
- Each row has an Edit button (opens an edit panel below the list, all
  fields editable) and a Send Mail button (opens a panel below the list
  showing every field read-only EXCEPT email/subject/body, which are
  editable).
- Hitting "Send" in the mail panel sends the email through Gmail (SMTP,
  from the account set by SENDER_EMAIL in .env) and -- only on a successful
  send -- persists the edited email/subject/body back to the database and
  increments times_contacted.

Setup:
  1. In Google Account settings, enable 2-Step Verification, then create an
     "App Password" for Mail: https://myaccount.google.com/apppasswords
  2. Add to .env (in the project root, next to this gen-leads/ folder):
       SENDER_EMAIL="<the Gmail address to send from>"
       GMAIL_APP_PASSWORD="<that 16-character app password, for the same account>"

Backup DB button:
  Uploads leads.db to the S3 bucket named by the S3_BUCKET env var, as
  leads<yyyyMMdd>.db (relies on AWS credentials being available via boto3's
  normal credential chain, e.g. ~/.aws/credentials).

Safety / testing:
  Set the environment variable LEADS_GUI_DRY_RUN=1 to make "Send" print the
  email to the console instead of actually sending it (the DB is still
  updated, exactly as on a real send). Use this for any testing -- never
  send real mail from a test run.

Run:
    python leads_app.py
"""

import os
import queue
import re
import smtplib
import sys
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import boto3
import markdown

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

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import CATEGORIES, Lead, get_engine, DEFAULT_DB  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from verify_email import verify_email_smtp  # noqa: E402

load_dotenv()

SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "dschellberg@gmail.com")
SENDER_DISPLAY = f"Succinct Solutions <{SENDER_EMAIL}>"
DRY_RUN = os.environ.get("LEADS_GUI_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

ROWS_VISIBLE = 10
ROW_HEIGHT = 34
COLS = [
    ("name", "Name", 280),
    ("city", "City", 120),
    ("state", "State", 55),
    ("category", "Category", 170),
    ("ranking", "Rank", 55),
    ("times_contacted", "Contacted", 90),
    ("status", "Status", 70),
    ("website", "Website", 420),
    ("email", "Email", 280),
]
ALL_CATEGORIES_LABEL = "All Categories"
BUTTON_AREA_WIDTH = 260  # reserved space for the Edit / Send Mail / Disable buttons
SCROLLBAR_WIDTH = 16  # tk.Scrollbar (not ttk) so this is an exact, known pixel value
TABLE_WIDTH = sum(width for _, _, width in COLS) + BUTTON_AREA_WIDTH  # full (scrollable) content width
VIEWPORT_WIDTH = 1300  # visible width; wider tables scroll horizontally instead of growing the window


def markdown_body_to_html(body: str) -> str:
    """Renders a Markdown email body (see email_example.md's style -- bold,
    bullet lists, and two-trailing-space hard breaks) to a self-contained
    HTML fragment suitable for a text/html email part.
    """
    rendered = markdown.markdown(body, extensions=["extra", "sane_lists"])
    return (
        '<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; '
        'line-height: 1.5; color: #1a1a1a;">\n' + rendered + "\n</div>"
    )


def send_email_via_gmail(to_addr: str, subject: str, body: str) -> None:
    """Sends one email through Gmail SMTP. Raises on failure.

    The body is authored as Markdown; this sends it as a multipart/alternative
    message so most clients render the formatted HTML version (bold, bullet
    lists, etc.), with the raw Markdown text kept as the plain-text fallback.

    Honors LEADS_GUI_DRY_RUN -- when set, this only prints what it would
    have sent and never opens a network connection.
    """
    if DRY_RUN:
        print(
            f"[DRY RUN] would send email\n  To: {to_addr}\n  From: {SENDER_DISPLAY}\n"
            f"  Subject: {subject}\n  Body:\n{body}\n"
        )
        return

    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD is not set in .env. Create a Gmail App Password "
            "(https://myaccount.google.com/apppasswords) and add it."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_DISPLAY
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(markdown_body_to_html(body), "html"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(SENDER_EMAIL, app_password)
        server.sendmail(SENDER_EMAIL, [to_addr], msg.as_string())


def backup_db_to_s3() -> str:
    """Uploads leads.db to the S3 bucket named by the S3_BUCKET env var, as
    leads<yyyyMMdd>.db. Returns the S3 key uploaded to. Raises on failure.
    """
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set in .env.")
    if not os.path.exists(DEFAULT_DB):
        raise RuntimeError(f"Database file not found: {DEFAULT_DB}")

    key = f"leads{datetime.now().strftime('%Y%m%d')}.db"
    boto3.client("s3").upload_file(DEFAULT_DB, bucket, key)
    return key


class ScrollableFrame(ttk.Frame):
    """A canvas + inner frame that scrolls vertically (always) and
    horizontally (only when content is wider than width_px -- e.g. a wide
    table). Fixed pixel viewport width (not stretch-to-fill) so it can be
    lined up exactly with a same-width header built independently above it.

    Other canvases (e.g. a fixed header row) can be kept in horizontal sync
    via sync_horizontal_with() -- they'll scroll left/right together with
    this one, using this frame's horizontal scrollbar.
    """

    def __init__(self, parent, width_px: int, height_px: int):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=width_px, height=height_px, highlightthickness=0)
        self.vscroll = tk.Scrollbar(self, orient="vertical", width=SCROLLBAR_WIDTH, command=self.canvas.yview)
        self.hscroll = tk.Scrollbar(self, orient="horizontal", width=SCROLLBAR_WIDTH, command=self._xview)
        self.inner = ttk.Frame(self.canvas)
        self._x_listeners: list[tk.Canvas] = []

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vscroll.set, xscrollcommand=self.hscroll.set)

        def _bind_wheel(_e):
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
            self.canvas.bind_all("<Shift-MouseWheel>", self._on_shift_mousewheel)

        def _unbind_wheel(_e):
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Shift-MouseWheel>")

        self.canvas.bind("<Enter>", _bind_wheel)
        self.canvas.bind("<Leave>", _unbind_wheel)

        self.columnconfigure(0, weight=0)
        self.canvas.grid(row=0, column=0, sticky="nw")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        self.hscroll.grid(row=1, column=0, sticky="ew")

    def _xview(self, *args):
        self.canvas.xview(*args)
        for other in self._x_listeners:
            other.xview(*args)

    def sync_horizontal_with(self, other_canvas: tk.Canvas):
        """other_canvas will be scrolled left/right in lockstep with this frame."""
        self._x_listeners.append(other_canvas)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_shift_mousewheel(self, event):
        self._xview("scroll", int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


class VScrollableFrame(ttk.Frame):
    """A vertically-scrollable frame that fills whatever space its parent
    gives it (unlike ScrollableFrame's fixed pixel size, used for the table).

    Used for the edit/send detail panel area: those panels can get tall
    (many fields, a multi-line body, etc.) and won't always fit below the
    table within the window -- this makes sure the Save/Send/Cancel buttons
    stay reachable via a scrollbar instead of being cut off.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", width=SCROLLBAR_WIDTH, command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="n")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        # keep the inner frame exactly as wide as the visible canvas, so
        # panels packed with anchor="center" inside it stay centered
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.window_id, width=e.width))

        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


class LeadsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Leads")
        self._center_window(min(VIEWPORT_WIDTH, TABLE_WIDTH) + 60, 760)

        self.engine = get_engine(DEFAULT_DB)
        self.sort_mode = tk.StringVar(value="natural")
        self.max_times_contacted = tk.StringVar(value="0")
        self.show_mode = tk.StringVar(value="active")  # "active" = not disabled, "all" = everything
        self.processed_mode = tk.StringVar(value="processed")  # "processed" or "unprocessed"
        self.search_text = tk.StringVar(value="")
        self.category_filter = tk.StringVar(value=ALL_CATEGORIES_LABEL)

        self._build_top_bar()

        # Header and the scrollable row list share one parent so they end up
        # pixel-identical -- that's what keeps the header columns lined up
        # with the row columns beneath them. Both are clipped to
        # VIEWPORT_WIDTH; when the table (TABLE_WIDTH) is wider than that,
        # the row list's horizontal scrollbar drags the header along with it
        # (see sync_horizontal_with below) instead of growing the window.
        table = ttk.Frame(self.root)
        table.pack(anchor="center")
        self._build_header_row(table)
        self.scroll_area = ScrollableFrame(table, width_px=VIEWPORT_WIDTH, height_px=ROW_HEIGHT * ROWS_VISIBLE)
        self.scroll_area.pack(anchor="w")
        self.scroll_area.sync_horizontal_with(self.header_canvas)

        self.detail_container = VScrollableFrame(self.root)
        self.detail_container.pack(fill="both", expand=True, padx=8, pady=(8, 8))

        self.refresh()

    # ---------- top controls ----------

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=8)

        row1 = ttk.Frame(bar)
        row1.pack(anchor="center")

        ttk.Label(row1, text="Sort by:").pack(side="left")
        natural_rb = ttk.Radiobutton(
            row1, text="Natural order", variable=self.sort_mode, value="natural", command=self.refresh
        )
        ranking_rb = ttk.Radiobutton(
            row1, text="Ranking (high → low)", variable=self.sort_mode, value="ranking", command=self.refresh
        )
        natural_rb.pack(side="left", padx=(6, 0))
        ranking_rb.pack(side="left", padx=(6, 0))

        ttk.Label(row1, text="Show:").pack(side="left", padx=(18, 0))
        active_rb = ttk.Radiobutton(
            row1, text="Not disabled", variable=self.show_mode, value="active", command=self.refresh
        )
        all_rb = ttk.Radiobutton(row1, text="All rows", variable=self.show_mode, value="all", command=self.refresh)
        active_rb.pack(side="left", padx=(6, 0))
        all_rb.pack(side="left", padx=(6, 0))

        ttk.Label(row1, text="Processed:").pack(side="left", padx=(18, 0))
        processed_rb = ttk.Radiobutton(
            row1, text="Processed", variable=self.processed_mode, value="processed", command=self.refresh
        )
        unprocessed_rb = ttk.Radiobutton(
            row1, text="Not processed", variable=self.processed_mode, value="unprocessed", command=self.refresh
        )
        processed_rb.pack(side="left", padx=(6, 0))
        unprocessed_rb.pack(side="left", padx=(6, 0))

        ttk.Label(row1, text="Category:").pack(side="left", padx=(18, 0))
        category_combo = ttk.Combobox(
            row1,
            textvariable=self.category_filter,
            values=[ALL_CATEGORIES_LABEL, *CATEGORIES],
            state="readonly",
            width=22,
        )
        category_combo.pack(side="left", padx=(6, 0))
        category_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        row2 = ttk.Frame(bar)
        row2.pack(anchor="center", pady=(6, 0))

        ttk.Label(row2, text="Times contacted ≤").pack(side="left")
        filter_entry = ttk.Entry(row2, textvariable=self.max_times_contacted, width=5, justify="center")
        filter_entry.pack(side="left", padx=(6, 0))
        filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Button(row2, text="Clear", command=self._clear_filter).pack(side="left", padx=(4, 0))

        ttk.Label(row2, text="Search").pack(side="left", padx=(18, 0))
        search_entry = ttk.Entry(row2, textvariable=self.search_text, width=20)
        search_entry.pack(side="left", padx=(6, 0))
        search_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Button(row2, text="Clear", command=self._clear_search).pack(side="left", padx=(4, 0))

        ttk.Button(row2, text="Apply", command=self.refresh).pack(side="left", padx=(12, 0))

        self.backup_btn = ttk.Button(row2, text="Backup DB", command=self._backup_db)
        self.backup_btn.pack(side="left", padx=(18, 0))

        self.count_label = ttk.Label(row2, text="")
        self.count_label.pack(side="left", padx=(18, 0))
        if DRY_RUN:
            ttk.Label(row2, text="DRY RUN MODE — no email will be sent", foreground="#a15c00").pack(
                side="left", padx=(18, 0)
            )

    @staticmethod
    def _make_cell(parent, text: str, width_px: int, *, bold: bool = False) -> tk.Widget:
        """A fixed pixel-width cell. Using an exact pixel width (rather than
        a character count, which renders differently for bold vs. normal
        text) is what keeps header and row columns pixel-aligned.

        Header cells (bold=True) are plain Labels -- just short static
        titles. Data cells are read-only Entry widgets instead of Labels so
        their value can be selected and copied (Labels don't support text
        selection at all); Ctrl+A selects the whole field. The full,
        untruncated value is always inserted -- Entry scrolls internally
        rather than needing ellipsis truncation, so a select-all + copy
        always grabs the complete value even if it's wider than the cell.
        """
        cell = tk.Frame(parent, width=width_px, height=ROW_HEIGHT)
        cell.pack_propagate(False)
        cell.pack(side="left")
        if bold:
            widget = ttk.Label(cell, text=text, anchor="center", justify="center", font=("", 9, "bold"))
            widget.pack(fill="both", expand=True)
        else:
            widget = tk.Entry(
                cell, justify="center", relief="flat", borderwidth=0, highlightthickness=0, font=("", 9)
            )
            widget.insert(0, text)
            widget.configure(state="readonly", readonlybackground=cell.cget("background"))
            widget.bind("<Control-a>", lambda e: (e.widget.select_range(0, "end"), "break")[1])
            widget.pack(fill="both", expand=True, padx=1)
        return widget

    def _build_header_row(self, parent):
        # A viewport-width canvas (matching the row list's canvas exactly) so
        # it can be scrolled left/right in sync with the rows. Its inner
        # frame is TABLE_WIDTH -- the *full* content width, identical to
        # every row's frame -- which is what keeps columns pixel-aligned at
        # any scroll position.
        row_holder = ttk.Frame(parent)
        row_holder.pack(anchor="w")
        self.header_canvas = tk.Canvas(row_holder, width=VIEWPORT_WIDTH, height=ROW_HEIGHT, highlightthickness=0)
        self.header_canvas.pack(side="left")
        # blank corner spacer sitting above the row list's vertical scrollbar
        tk.Frame(row_holder, width=SCROLLBAR_WIDTH, height=ROW_HEIGHT).pack(side="left")

        header = tk.Frame(self.header_canvas, width=TABLE_WIDTH, height=ROW_HEIGHT)
        header.pack_propagate(False)
        self.header_canvas.create_window((0, 0), window=header, anchor="nw")
        header.bind(
            "<Configure>", lambda e: self.header_canvas.configure(scrollregion=self.header_canvas.bbox("all"))
        )
        # leading spacer covers the button column in every row, so the
        # header's total scrollable width matches each row's exactly.
        self._make_cell(header, "", BUTTON_AREA_WIDTH, bold=True)
        for _, label, width in COLS:
            self._make_cell(header, label, width, bold=True)

    # ---------- data ----------

    def _clear_filter(self):
        self.max_times_contacted.set("")
        self.refresh()

    def _clear_search(self):
        self.search_text.set("")
        self.refresh()

    def load_leads(self) -> list[Lead]:
        with Session(self.engine) as session:
            query = session.query(Lead)

            if self.show_mode.get() == "active":
                query = query.filter(Lead.disabled.is_(False))

            if self.processed_mode.get() == "processed":
                query = query.filter(Lead.processed.is_(True))
            else:
                query = query.filter(Lead.processed.is_(False))

            raw = self.max_times_contacted.get().strip()
            if raw:
                try:
                    threshold = int(raw)
                    if threshold < 0:
                        raise ValueError
                except ValueError:
                    messagebox.showerror(
                        "Invalid filter", "\"Times contacted ≤\" must be a non-negative whole number."
                    )
                else:
                    query = query.filter(Lead.times_contacted <= threshold)

            needle = self.search_text.get().strip()
            if needle:
                # ilike -- case-insensitive substring match against the
                # company name only ("phi" matches "Philadelphia Eagles").
                query = query.filter(Lead.name.ilike(f"%{needle}%"))

            category = self.category_filter.get()
            if category and category != ALL_CATEGORIES_LABEL:
                query = query.filter(Lead.category == category)

            if self.sort_mode.get() == "ranking":
                query = query.order_by(Lead.ranking.desc(), Lead.id.asc())
            else:
                query = query.order_by(Lead.id.asc())
            leads = query.all()
            session.expunge_all()
            return leads

    def refresh(self):
        self.close_detail_panel()
        leads = self.load_leads()
        self.count_label.config(text=f"{len(leads)} companies")
        self.scroll_area.clear()
        for lead in leads:
            self._build_row(self.scroll_area.inner, lead)

    def _build_row(self, parent, lead: Lead):
        row = tk.Frame(parent, width=TABLE_WIDTH, height=ROW_HEIGHT)
        row.pack_propagate(False)
        row.pack()

        btn_area = tk.Frame(row, width=BUTTON_AREA_WIDTH, height=ROW_HEIGHT)
        btn_area.pack_propagate(False)
        btn_area.pack(side="left")
        ttk.Button(btn_area, text="Edit", width=6, command=lambda lid=lead.id: self.open_edit_panel(lid)).pack(
            side="left", padx=(8, 2), pady=4
        )
        ttk.Button(
            btn_area, text="Send Mail", width=10, command=lambda lid=lead.id: self.open_send_panel(lid)
        ).pack(side="left", padx=2, pady=4)
        ttk.Button(
            btn_area,
            text="Enable" if lead.disabled else "Disable",
            width=8,
            command=lambda lid=lead.id: self.toggle_disabled(lid),
        ).pack(side="left", padx=2, pady=4)

        values = {
            "name": lead.name,
            "city": lead.city,
            "state": lead.state,
            "category": lead.category,
            "ranking": str(lead.ranking),
            "times_contacted": str(lead.times_contacted),
            "status": "Disabled" if lead.disabled else "Active",
            "website": lead.website,
            "email": lead.email,
        }
        for key, _, width in COLS:
            widget = self._make_cell(row, values[key], width)
            if key == "status" and lead.disabled:
                widget.configure(foreground="#a15c00")

    def toggle_disabled(self, lead_id: int):
        with Session(self.engine) as session:
            lead = self._get_lead(session, lead_id)
            lead.disabled = not lead.disabled
            session.commit()
        self.refresh()

    # ---------- backup ----------

    def _backup_db(self):
        self.backup_btn.config(state="disabled")
        self.count_label.config(text="Backing up to S3…")
        result_queue: "queue.Queue[tuple[bool, str]]" = queue.Queue()
        threading.Thread(target=self._backup_db_worker, args=(result_queue,), daemon=True).start()
        self._poll_backup_queue(result_queue)

    @staticmethod
    def _backup_db_worker(result_queue: "queue.Queue[tuple[bool, str]]"):
        try:
            key = backup_db_to_s3()
            result_queue.put((True, key))
        except Exception as e:
            result_queue.put((False, str(e)))

    def _poll_backup_queue(self, result_queue: "queue.Queue[tuple[bool, str]]"):
        try:
            ok, message = result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, lambda: self._poll_backup_queue(result_queue))
            return

        self.backup_btn.config(state="normal")
        leads = self.load_leads()
        self.count_label.config(text=f"{len(leads)} companies")

        if ok:
            messagebox.showinfo("Backup complete", f"Uploaded to S3 as {message}.")
        else:
            messagebox.showerror("Backup failed", message)

    # ---------- detail panel plumbing ----------

    def close_detail_panel(self):
        self.detail_container.clear()

    def _get_lead(self, session: Session, lead_id: int) -> Lead:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise LookupError(f"Lead {lead_id} no longer exists")
        return lead

    # ---------- edit panel ----------

    def open_edit_panel(self, lead_id: int):
        self.close_detail_panel()
        with Session(self.engine) as session:
            lead = self._get_lead(session, lead_id)

            panel = ttk.LabelFrame(self.detail_container.inner, text=f"Edit — {lead.name}")
            panel.pack(anchor="center", pady=4)

            fields = {}

            def add_entry(row_i, label, attr, width=60):
                ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
                e = ttk.Entry(panel, width=width)
                value = getattr(lead, attr)
                e.insert(0, "" if value is None else str(value))
                e.grid(row=row_i, column=1, sticky="w", padx=6, pady=4)
                fields[attr] = e

            def add_text(row_i, label, attr, height=4):
                ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
                t = tk.Text(panel, width=60, height=height, wrap="word")
                value = getattr(lead, attr)
                t.insert("1.0", "" if value is None else str(value))
                t.grid(row=row_i, column=1, sticky="w", padx=6, pady=4)
                fields[attr] = t

            def add_combo(row_i, label, attr, values):
                ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
                var = tk.StringVar(value=getattr(lead, attr) or "")
                c = ttk.Combobox(panel, textvariable=var, values=values, width=57)
                c.grid(row=row_i, column=1, sticky="w", padx=6, pady=4)
                fields[attr] = var

            add_entry(0, "Name", "name")
            add_entry(1, "City", "city", width=25)
            add_entry(2, "State", "state", width=10)
            add_text(3, "Description", "description", height=3)
            add_combo(4, "Category", "category", CATEGORIES)
            add_entry(5, "Ranking (1-10)", "ranking", width=6)
            add_entry(6, "Website", "website")
            add_entry(7, "Email", "email")
            add_entry(8, "Subject", "subject")
            add_text(9, "Body", "body", height=6)
            add_entry(10, "Times contacted", "times_contacted", width=6)

            ttk.Label(panel, text="Disabled").grid(row=11, column=0, sticky="ne", padx=6, pady=4)
            disabled_var = tk.BooleanVar(value=lead.disabled)
            ttk.Checkbutton(panel, variable=disabled_var).grid(row=11, column=1, sticky="w", padx=6, pady=4)
            fields["disabled"] = disabled_var

            verify_status = ttk.Label(panel, text="", foreground="#a15c00", wraplength=420, justify="left")
            verify_status.grid(row=13, column=0, columnspan=2, padx=6)

            btn_row = ttk.Frame(panel)
            btn_row.grid(row=12, column=0, columnspan=2, pady=10)
            ttk.Button(btn_row, text="Save", command=lambda: self._save_edit(lead_id, fields)).pack(
                side="left", padx=6
            )
            verify_btn = ttk.Button(
                btn_row,
                text="Verify Email",
                command=lambda: self._verify_email(fields, verify_btn, verify_status),
            )
            verify_btn.pack(side="left", padx=6)
            ttk.Button(btn_row, text="Cancel", command=self.close_detail_panel).pack(side="left", padx=6)

    def _save_edit(self, lead_id: int, fields: dict):
        try:
            ranking = int(fields["ranking"].get().strip())
            if not (1 <= ranking <= 10):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid value", "Ranking must be a whole number from 1 to 10.")
            return
        try:
            times_contacted = int(fields["times_contacted"].get().strip())
            if times_contacted < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid value", "Times contacted must be a non-negative whole number.")
            return

        with Session(self.engine) as session:
            lead = self._get_lead(session, lead_id)
            lead.name = fields["name"].get().strip()
            lead.city = fields["city"].get().strip()
            lead.state = fields["state"].get().strip()
            lead.description = fields["description"].get("1.0", "end").strip()
            lead.category = fields["category"].get().strip()
            lead.ranking = ranking
            lead.website = fields["website"].get().strip()
            lead.email = fields["email"].get().strip()
            lead.subject = fields["subject"].get().strip()
            lead.body = fields["body"].get("1.0", "end").strip()
            lead.times_contacted = times_contacted
            lead.disabled = fields["disabled"].get()
            session.commit()

        self.refresh()

    def _verify_email(self, fields: dict, verify_btn: ttk.Button, status_label: ttk.Label):
        email = fields["email"].get().strip()
        if not email or "@" not in email:
            messagebox.showerror("Invalid email", "That doesn't look like a valid email address.")
            return

        verify_btn.config(state="disabled")
        candidate_count = len([c for c in email.split(",") if c.strip()])
        note = f" ({candidate_count} candidates)" if candidate_count > 1 else ""
        status_label.config(
            text=f"Verifying {email}{note} ... this makes a live SMTP connection and can take up to ~10 seconds."
        )
        result_queue: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._verify_email_worker, args=(email, result_queue), daemon=True).start()
        self._poll_verify_queue(result_queue, verify_btn, status_label, fields["email"])

    @staticmethod
    def _verify_email_worker(email: str, result_queue: "queue.Queue[str]"):
        try:
            result = verify_email_smtp(email)
        except Exception as e:
            result = f"Unexpected error: {e}"
        result_queue.put(result)

    def _poll_verify_queue(
        self, result_queue: "queue.Queue[str]", verify_btn: ttk.Button, status_label: ttk.Label, email_entry: tk.Entry
    ):
        try:
            result = result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, lambda: self._poll_verify_queue(result_queue, verify_btn, status_label, email_entry))
            return
        # the edit panel may have been closed/replaced while the background
        # SMTP probe was still running -- don't touch widgets that no longer exist.
        if status_label.winfo_exists():
            status_label.config(text=result)
        if verify_btn.winfo_exists():
            verify_btn.config(state="normal")

        # verify_email_smtp() returns "Valid: <email> ..." (same format for a
        # single address or the winner among comma-delimited candidates) --
        # once one address is confirmed, collapse the field down to just it.
        match = re.match(r"^Valid: (\S+) ", result)
        if match and email_entry.winfo_exists():
            email_entry.delete(0, "end")
            email_entry.insert(0, match.group(1))

    # ---------- send-mail panel ----------

    def open_send_panel(self, lead_id: int):
        self.close_detail_panel()
        with Session(self.engine) as session:
            lead = self._get_lead(session, lead_id)

            panel = ttk.LabelFrame(self.detail_container.inner, text=f"Send Mail — {lead.name}")
            panel.pack(anchor="center", pady=4)

            def add_readonly(row_i, label, value):
                ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
                ttk.Label(panel, text=str(value), wraplength=420, justify="left").grid(
                    row=row_i, column=1, sticky="w", padx=6, pady=4
                )

            add_readonly(0, "Name", lead.name)
            add_readonly(1, "City / State", f"{lead.city}, {lead.state}")
            add_readonly(2, "Ranking", lead.ranking)
            add_readonly(3, "Description", lead.description)
            add_readonly(4, "Website", lead.website)
            add_readonly(5, "Times contacted", lead.times_contacted)
            add_readonly(6, "Status", "Disabled" if lead.disabled else "Active")

            ttk.Label(panel, text="Email").grid(row=7, column=0, sticky="ne", padx=6, pady=4)
            email_entry = ttk.Entry(panel, width=60)
            email_entry.insert(0, lead.email)
            email_entry.grid(row=7, column=1, sticky="w", padx=6, pady=4)

            ttk.Label(panel, text="Subject").grid(row=8, column=0, sticky="ne", padx=6, pady=4)
            subject_entry = ttk.Entry(panel, width=60)
            subject_entry.insert(0, lead.subject)
            subject_entry.grid(row=8, column=1, sticky="w", padx=6, pady=4)

            ttk.Label(panel, text="Body").grid(row=9, column=0, sticky="ne", padx=6, pady=4)
            body_text = tk.Text(panel, width=60, height=8, wrap="word")
            body_text.insert("1.0", lead.body)
            body_text.grid(row=9, column=1, sticky="w", padx=6, pady=4)

            status_label = ttk.Label(panel, text="", foreground="#a15c00")
            status_label.grid(row=10, column=0, columnspan=2)

            btn_row = ttk.Frame(panel)
            btn_row.grid(row=11, column=0, columnspan=2, pady=10)
            send_btn = ttk.Button(
                btn_row,
                text="Send",
                command=lambda: self._send(lead_id, email_entry, subject_entry, body_text, status_label, send_btn),
            )
            send_btn.pack(side="left", padx=6)
            ttk.Button(btn_row, text="Cancel", command=self.close_detail_panel).pack(side="left", padx=6)

    def _send(self, lead_id, email_entry, subject_entry, body_text, status_label, send_btn):
        to_addr = email_entry.get().strip()
        subject = subject_entry.get().strip()
        body = body_text.get("1.0", "end").strip()

        if "@" not in to_addr:
            messagebox.showerror("Invalid email", "That doesn't look like a valid email address.")
            return
        if not subject or not body:
            messagebox.showerror("Missing content", "Subject and body can't be empty.")
            return

        send_btn.config(state="disabled")
        status_label.config(text="Sending…")
        self.root.update_idletasks()

        try:
            send_email_via_gmail(to_addr, subject, body)
        except Exception as e:
            send_btn.config(state="normal")
            status_label.config(text="")
            messagebox.showerror("Send failed", f"The email was NOT sent, and nothing was saved.\n\n{e}")
            return

        with Session(self.engine) as session:
            lead = self._get_lead(session, lead_id)
            lead.email = to_addr
            lead.subject = subject
            lead.body = body
            lead.times_contacted = (lead.times_contacted or 0) + 1
            session.commit()

        note = " (dry run — not actually sent)" if DRY_RUN else ""
        messagebox.showinfo("Sent", f"Email sent to {to_addr}{note}.")
        self.refresh()


def main():
    root = tk.Tk()
    LeadsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
