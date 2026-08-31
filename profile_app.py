"""
Standalone desktop app for managing Profile rows -- the sender email plus
the overview/decision-maker/signature-block texts that lead_gen.py drafts
under (see db.py's Profile model). Each Lead belongs to one Profile, so a
single leads.db can serve multiple businesses/users at once.

- Lists every profile (email + short previews of the three texts).
- New Profile / Edit / Delete buttons. Deleting a profile clears
  profile_id on any leads that pointed to it (they fall back to
  lead_gen.resolve_lead_profile()'s auto-assignment next time they're
  processed -- which only works if exactly one profile remains).

Run:
    python profile_app.py
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
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DEFAULT_DB, Profile, get_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _preview(text: str, length: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


class ProfileApp:
    def __init__(self, root: tk.Misc):
        self.root = root
        # root is a real window (standalone run) or a plain Frame embedded
        # in main_app.py's content area (normal flow, one consolidated
        # window) -- only a real window has .title()/.geometry() to set.
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("Profiles")
            self._center_window(760, 560)

        self.engine = get_engine(DEFAULT_DB)
        self.on_change = None  # optional callback, set by main_app.py, fired after any save/delete

        self._build_list()
        self._build_detail_container()
        self.refresh()

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _build_list(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Profiles", font=("", 11, "bold")).pack(side="left")
        ttk.Button(top, text="New Profile", command=self.open_new_panel).pack(side="right")

        columns = ("email", "overview", "decision_maker", "signature_block")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=8, selectmode="browse")
        self.tree.heading("email", text="Email")
        self.tree.heading("overview", text="Overview")
        self.tree.heading("decision_maker", text="Decision Maker")
        self.tree.heading("signature_block", text="Signature Block")
        self.tree.column("email", width=200, anchor="w")
        self.tree.column("overview", width=220, anchor="w")
        self.tree.column("decision_maker", width=220, anchor="w")
        self.tree.column("signature_block", width=160, anchor="w")
        self.tree.pack(fill="x", padx=10, pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        btn_row = ttk.Frame(self.root)
        btn_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(btn_row, text="Edit", command=self.open_edit_panel).pack(side="left")
        ttk.Button(btn_row, text="Delete", command=self.on_delete).pack(side="left", padx=(6, 0))

    def _build_detail_container(self):
        self.detail_container = ttk.Frame(self.root)
        self.detail_container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def refresh(self):
        self.close_detail_panel()
        for row in self.tree.get_children():
            self.tree.delete(row)
        with Session(self.engine) as session:
            for profile in session.query(Profile).order_by(Profile.id.asc()).all():
                self.tree.insert(
                    "",
                    "end",
                    iid=str(profile.id),
                    values=(
                        profile.email,
                        _preview(profile.overview),
                        _preview(profile.decision_maker),
                        _preview(profile.signature_block),
                    ),
                )
        if self.on_change:
            self.on_change()

    def _selected_id(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _on_select(self, _event):
        pass  # selection alone doesn't open anything -- Edit/Delete act on it explicitly

    # ---------- detail panel ----------

    def close_detail_panel(self):
        for child in self.detail_container.winfo_children():
            child.destroy()

    def open_new_panel(self):
        self._open_form(profile=None)

    def open_edit_panel(self):
        profile_id = self._selected_id()
        if profile_id is None:
            messagebox.showinfo("No selection", "Select a profile first.")
            return
        with Session(self.engine) as session:
            profile = session.get(Profile, profile_id)
            if profile is None:
                messagebox.showerror("Not found", "That profile no longer exists.")
                self.refresh()
                return
            self._open_form(profile=profile)

    def _open_form(self, profile: Profile | None):
        self.close_detail_panel()
        is_new = profile is None
        title = "New Profile" if is_new else f"Edit — {profile.email}"
        panel = ttk.LabelFrame(self.detail_container, text=title)
        panel.pack(fill="both", expand=True)

        fields = {}

        def add_entry(row_i, label, attr, width=60):
            ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
            e = ttk.Entry(panel, width=width)
            if profile is not None:
                e.insert(0, getattr(profile, attr) or "")
            e.grid(row=row_i, column=1, sticky="w", padx=6, pady=4)
            fields[attr] = e

        def add_text(row_i, label, attr, height=5):
            ttk.Label(panel, text=label).grid(row=row_i, column=0, sticky="ne", padx=6, pady=4)
            t = tk.Text(panel, width=60, height=height, wrap="word")
            if profile is not None:
                t.insert("1.0", getattr(profile, attr) or "")
            t.grid(row=row_i, column=1, sticky="w", padx=6, pady=4)
            fields[attr] = t

        add_entry(0, "Email", "email")
        add_text(1, "Overview", "overview", height=6)
        add_text(2, "Decision Maker", "decision_maker", height=3)
        add_text(3, "Signature Block", "signature_block", height=5)

        hint = ttk.Label(
            panel,
            text="Overview: your company/offering, fed into fit-ranking + drafting.\n"
            "Decision Maker: who to search for, e.g. \"a technical decision-maker (CTO, VP of Engineering, "
            "Head of Technology, or Director of IT/Engineering)\".\n"
            "Signature Block: the email closing -- end every line but the last with two trailing spaces "
            "(Markdown hard break) or it renders as one run-on line.",
            foreground="#666666",
            wraplength=560,
            justify="left",
        )
        hint.grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 8))

        btn_row = ttk.Frame(panel)
        btn_row.grid(row=5, column=0, columnspan=2, pady=10)
        profile_id = None if is_new else profile.id
        ttk.Button(btn_row, text="Save", command=lambda: self._save(profile_id, fields)).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Cancel", command=self.close_detail_panel).pack(side="left", padx=6)

    def _save(self, profile_id: int | None, fields: dict):
        email = fields["email"].get().strip()
        if not email or "@" not in email:
            messagebox.showerror("Invalid email", "That doesn't look like a valid email address.")
            return
        overview = fields["overview"].get("1.0", "end").strip()
        decision_maker = fields["decision_maker"].get("1.0", "end").strip()
        signature_block = fields["signature_block"].get("1.0", "end").strip()

        with Session(self.engine) as session:
            if profile_id is None:
                session.add(
                    Profile(
                        email=email, overview=overview, decision_maker=decision_maker, signature_block=signature_block
                    )
                )
            else:
                profile = session.get(Profile, profile_id)
                if profile is None:
                    messagebox.showerror("Not found", "That profile no longer exists.")
                    self.refresh()
                    return
                profile.email = email
                profile.overview = overview
                profile.decision_maker = decision_maker
                profile.signature_block = signature_block
            session.commit()

        self.refresh()

    def on_delete(self):
        profile_id = self._selected_id()
        if profile_id is None:
            messagebox.showinfo("No selection", "Select a profile first.")
            return
        with Session(self.engine) as session:
            profile = session.get(Profile, profile_id)
            if profile is None:
                self.refresh()
                return
            lead_count = len(profile.leads)
            email = profile.email

        note = f"\n\n{lead_count} lead(s) are linked to it -- they'll be left with no profile." if lead_count else ""
        if not messagebox.askyesno("Delete profile", f"Delete the profile for {email!r}?{note}"):
            return

        with Session(self.engine) as session:
            profile = session.get(Profile, profile_id)
            if profile is not None:
                for lead in list(profile.leads):
                    lead.profile_id = None
                session.delete(profile)
                session.commit()
        self.refresh()


def main():
    root = tk.Tk()
    ProfileApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
