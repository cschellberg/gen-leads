"""
Standalone desktop app for the verify_email.py SMTP-based email checker.

Enter an address, hit Verify, see the result in the text area below. The
actual SMTP handshake (verify_email.verify_email_smtp) runs on a background
thread so the window never freezes -- it can take several seconds, and
outbound port 25 is blocked on plenty of networks (residential ISPs, many
corporate/cloud networks), in which case you'll get a connection-error
result rather than a definitive answer. That's the underlying probe's
normal behavior, not a bug in this app.

Run:
    python verify_email_app.py
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
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_email import verify_email_smtp  # noqa: E402


class VerifyEmailApp:
    def __init__(self, root: tk.Misc):
        self.root = root
        # root is a real window (standalone run) or a plain Frame embedded
        # in main_app.py's content area (normal flow, one consolidated
        # window) -- only a real window has .title()/.geometry() to set.
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("Verify Email")
            self._center_window(520, 380)

        self.result_queue: "queue.Queue[str]" = queue.Queue()

        top = ttk.Frame(root)
        top.pack(fill="x", padx=12, pady=12)

        ttk.Label(top, text="Email address:").pack(side="left")
        self.email_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.email_var, width=32)
        entry.pack(side="left", padx=(6, 6))
        entry.bind("<Return>", lambda e: self.on_verify())
        entry.focus_set()

        self.verify_btn = ttk.Button(top, text="Verify", command=self.on_verify)
        self.verify_btn.pack(side="left")

        result_frame = ttk.LabelFrame(root, text="Result")
        result_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.result_text = tk.Text(result_frame, wrap="word", height=12, state="disabled")
        self.result_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def set_result(self, text: str):
        self.result_text.config(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", text)
        self.result_text.config(state="disabled")

    def on_verify(self):
        email = self.email_var.get().strip()
        if not email or "@" not in email:
            self.set_result("Please enter a valid-looking email address (needs an '@').")
            return

        self.verify_btn.config(state="disabled")
        self.set_result(
            f"Verifying {email} ...\n\n"
            "This makes a live SMTP connection to the domain's mail server "
            "and can take up to ~10 seconds. If this network blocks "
            "outbound port 25 (common on home/office networks), you'll see "
            "a connection error below rather than a definitive answer."
        )
        threading.Thread(target=self._worker, args=(email,), daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _worker(self, email: str):
        try:
            result = verify_email_smtp(email)
        except Exception as e:
            result = f"Unexpected error: {e}"
        self.result_queue.put(result)

    def _poll_queue(self):
        try:
            result = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_queue)
            return
        self.set_result(result)
        self.verify_btn.config(state="normal")


def main():
    root = tk.Tk()
    VerifyEmailApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
