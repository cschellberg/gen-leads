"""
Standalone app: a scrollable, sortable view of the lead_runs table -- what
LinkedIn search URLs and page ranges have already been scraped, and when.

Read-only. For logging a new run or processing unprocessed leads, use
lead_runs_app.py instead.

Run:
    python lead_runs_viewer.py
"""

import os
import sys
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
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DEFAULT_DB, LeadRun, get_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

COLUMNS = [
    ("id", "ID", 50, "center"),
    ("date", "Date", 150, "center"),
    ("from_page", "From Page", 90, "center"),
    ("to_page", "To Page", 90, "center"),
    ("url", "URL", 520, "w"),
]


class LeadRunsViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lead Runs")
        self._center_window(900, 500)

        self.engine = get_engine(DEFAULT_DB)
        self.sort_column = "id"
        self.sort_reverse = True  # most recent first by default

        self._build_top_bar()
        self._build_table()
        self.refresh()

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        self.count_label = ttk.Label(bar, text="")
        self.count_label.pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="right")

    def _build_table(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        col_ids = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(frame, columns=col_ids, show="headings")
        for col_id, label, width, anchor in COLUMNS:
            self.tree.heading(col_id, text=label, command=lambda c=col_id: self.sort_by(c))
            self.tree.column(col_id, width=width, anchor=anchor)

        vscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def sort_by(self, column: str):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self.refresh()

    def refresh(self):
        with Session(self.engine) as session:
            runs = session.query(LeadRun).all()

        key_funcs = {
            "id": lambda r: r.id,
            "date": lambda r: r.run_date,
            "from_page": lambda r: r.from_page,
            "to_page": lambda r: r.to_page,
            "url": lambda r: r.url,
        }
        runs.sort(key=key_funcs[self.sort_column], reverse=self.sort_reverse)

        self.tree.delete(*self.tree.get_children())
        for run in runs:
            date_str = run.run_date.strftime("%Y-%m-%d %H:%M") if run.run_date else ""
            self.tree.insert("", "end", values=(run.id, date_str, run.from_page, run.to_page, run.url))

        self.count_label.config(text=f"{len(runs)} run(s)")


def main():
    root = tk.Tk()
    LeadRunsViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
