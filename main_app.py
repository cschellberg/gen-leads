"""
Parent launcher for the three standalone leads-toolkit apps -- lead_runs_app.py,
leads_app.py, and verify_email_app.py -- each still its own independent window
with its own logic, unchanged. This just gives them a single entry point and
an "Apps" menu bar to open any of them from.

Picking a menu item that's already open brings the existing window to the
front instead of opening a second copy.

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

APPS = {
    "lead_runs": ("Lead Runs", "lead_runs_app", "LeadRunsApp"),
    "leads": ("Leads", "leads_app", "LeadsApp"),
    "verify_email": ("Verify Email", "verify_email_app", "VerifyEmailApp"),
}


class MainApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Succinct Solutions — Leads Toolkit")
        self._center_window(420, 260)
        self.root.resizable(False, False)

        self.open_windows: dict[str, tk.Toplevel] = {}

        self._build_menu()
        self._build_body()

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

    def _build_body(self):
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(body, text="Succinct Solutions — Leads Toolkit", font=("", 12, "bold")).pack(pady=(0, 6))
        ttk.Label(body, text="Use the Apps menu above to open a tool.").pack(pady=(0, 12))

        for key, (label, _, _) in APPS.items():
            ttk.Button(body, text=label, command=lambda k=key: self.open_app(k)).pack(fill="x", pady=3)

    def open_app(self, key: str):
        win = self.open_windows.get(key)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return

        label, module_name, class_name = APPS[key]
        # imported lazily, on first open, not at module load -- lead_runs_app
        # in particular can pull in Tavily/Gemini client setup once its
        # "Process" button is used, which shouldn't happen just from opening
        # the launcher.
        import importlib

        module = importlib.import_module(module_name)
        app_cls = getattr(module, class_name)

        top = tk.Toplevel(self.root)
        top.protocol("WM_DELETE_WINDOW", lambda k=key, w=top: self._on_child_close(k, w))
        app_cls(top)
        self.open_windows[key] = top

    def _on_child_close(self, key: str, win: tk.Toplevel):
        del self.open_windows[key]
        win.destroy()


def main():
    root = tk.Tk()
    MainApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
