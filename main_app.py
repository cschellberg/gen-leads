"""
Single-window launcher for the leads-toolkit apps -- lead_runs_app.py,
leads_app.py, verify_email_app.py, and profile_app.py. There is only ever
one top-level window (this one); picking a tool from the Apps menu embeds
its UI into this window's content area below the Profile dropdown, in
place of whatever was there before -- no separate Toplevel windows.

Each app class is otherwise unchanged and still fully standalone (its own
`if __name__ == "__main__": main()` opens it in its own real window) --
the only difference when embedded here is that it's handed a plain Frame
instead of a Tk/Toplevel, so it skips setting a window title/geometry (see
each app's `__init__`).

Run:
    python main_app.py
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
from db import DEFAULT_DB, Profile, get_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

APP_TITLE = "Succinct Solutions — Leads Toolkit"

APPS = {
    "lead_runs": ("Lead Runs", "lead_runs_app", "LeadRunsApp"),
    "leads": ("Leads", "leads_app", "LeadsApp"),
    "verify_email": ("Verify Email", "verify_email_app", "VerifyEmailApp"),
    "profile": ("Manage Profiles", "profile_app", "ProfileApp"),
}
# Apps that care which Profile is currently selected -- get it passed in at
# open time and pushed live updates via set_active_profile_id() if the
# dropdown changes while they're the one currently embedded. lead_runs_app
# uses it to tag newly-scraped leads (see its Copy Scrape Prompt); leads_app
# uses it to filter the leads list down to that profile's leads (which
# lead is *sent* as is still governed by that lead's own linked Profile,
# not "the active one" -- see leads_app.py). verify_email has no lead/
# profile data at all, so it's never in this set.
PROFILE_AWARE_APPS = {"lead_runs", "leads"}

# Roughly each app's old standalone-window size, plus a bit for this
# window's menu bar / Profile dropdown it didn't have before -- generous
# enough to leave room for that app's own internal detail panels (e.g.
# profile_app's edit form, leads_app's edit/send panel) without needing to
# resize again once one of those appears. The window is always sized to fit
# the single largest of these (see MAIN_WINDOW_SIZE below) and never
# resizes when switching apps, so smaller apps just get extra margin around
# them rather than the window jumping to a different size each time.
APP_CONTENT_SIZE = {
    "lead_runs": (640, 700),
    "leads": (1360, 800),
    "verify_email": (520, 400),
    "profile": (760, 620),
}
MAIN_WINDOW_SIZE = (
    max(w for w, _ in APP_CONTENT_SIZE.values()),
    max(h for _, h in APP_CONTENT_SIZE.values()),
)


class MainApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.resizable(True, True)

        self.engine = get_engine(DEFAULT_DB)
        self.current_key: str | None = None
        self.app_instances: dict[str, object] = {}
        self._profile_ids: list[int] = []
        self.active_profile_id: int | None = None

        self._build_menu()
        self._build_top_bar()
        self._build_content_area()
        self.refresh_profiles()
        # Fixed for the whole session -- big enough for the largest app
        # (see MAIN_WINDOW_SIZE) -- so switching apps never changes the
        # window's size, only what's shown inside it.
        self._center_window(*MAIN_WINDOW_SIZE)

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        apps_menu = tk.Menu(menubar, tearoff=0)
        for key, (label, _, _) in APPS.items():
            apps_menu.add_command(label=label, command=lambda k=key: self.open_app(k))
        menubar.add_cascade(label="Apps", menu=apps_menu)
        self.root.config(menu=menubar)

    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=16, pady=(16, 8))
        ttk.Label(bar, text="Active profile:").pack(side="left")
        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(bar, textvariable=self.profile_var, state="readonly", width=28)
        self.profile_combo.pack(side="left", padx=(6, 0))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

    def _build_content_area(self):
        self.content_frame = ttk.Frame(self.root)
        self.content_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.placeholder = ttk.Label(self.content_frame, text="Use the Apps menu above to open a tool.")
        self.placeholder.pack(pady=40)

    # ---------- profile dropdown ----------

    def refresh_profiles(self):
        """Reloads the Profile dropdown from the database -- called on
        startup and whenever profile_app.py reports a change (new/edited/
        deleted profile), so the list and selection stay current.
        """
        with Session(self.engine) as session:
            profiles = session.query(Profile).order_by(Profile.id.asc()).all()
            self._profile_ids = [p.id for p in profiles]
            emails = [p.email for p in profiles]

        self.profile_combo["values"] = emails
        if not emails:
            self.profile_var.set("")
            self.active_profile_id = None
            return

        if self.active_profile_id in self._profile_ids:
            index = self._profile_ids.index(self.active_profile_id)
        else:
            index = 0
            self.active_profile_id = self._profile_ids[0]
        self.profile_combo.current(index)
        self._push_active_profile()

    def _on_profile_selected(self, _event=None):
        index = self.profile_combo.current()
        if index < 0 or index >= len(self._profile_ids):
            return
        self.active_profile_id = self._profile_ids[index]
        self._push_active_profile()

    def _push_active_profile(self):
        """Tells the currently-embedded app about the current selection, if
        it's a profile-aware one, so it doesn't keep using a stale id."""
        instance = self.app_instances.get(self.current_key)
        if instance is not None and hasattr(instance, "set_active_profile_id"):
            instance.set_active_profile_id(self.active_profile_id)

    # ---------- embedded app content ----------

    def open_app(self, key: str):
        if key == self.current_key:
            return  # already showing this one -- nothing to do

        for child in self.content_frame.winfo_children():
            child.destroy()
        self.app_instances.pop(self.current_key, None)
        self.current_key = None

        label, module_name, class_name = APPS[key]
        # imported lazily, on first open, not at module load -- lead_runs_app
        # in particular can pull in Gemini client setup once its "Process"
        # button is used, which shouldn't happen just from opening the
        # launcher.
        import importlib

        module = importlib.import_module(module_name)
        app_cls = getattr(module, class_name)

        if key in PROFILE_AWARE_APPS:
            instance = app_cls(self.content_frame, active_profile_id=self.active_profile_id)
        else:
            instance = app_cls(self.content_frame)
        if key == "profile":
            instance.on_change = self.refresh_profiles

        self.current_key = key
        self.app_instances[key] = instance
        self.root.title(f"{APP_TITLE} — {label}")


def main():
    root = tk.Tk()
    MainApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
