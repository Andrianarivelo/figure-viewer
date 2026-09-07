"""FigView - backward-compatible entry point.

Keeps the historical file name so the desktop shortcut, the .bat launcher and
any script that calls::

    pythonw.exe <app folder>\\figure_viewer_app.py "<folder or file>"

keeps working. All of the implementation lives in the ``figureviewer`` package
next to this file.

Because the app is normally started with ``pythonw.exe`` (no console), any
import or startup failure is written to ``figure_viewer_error.log`` and shown
in a message box instead of vanishing silently.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ERROR_LOG = APP_DIR / "figure_viewer_error.log"
DISPLAY_NAME = "FigView"

# Make sure the package next to this file wins over anything else on sys.path.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _report(title: str, message: str) -> None:
    """Log a startup failure and show it, even without a console."""
    try:
        ERROR_LOG.write_text(message, encoding="utf-8")
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message[:4000])
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    # Explorer launches should not pay for Tk/Pillow/document imports when an
    # existing viewer can accept their selection directly.
    from figureviewer.ipc import forward_arguments, startup_claim
    if forward_arguments(sys.argv[1:]):
        return 0
    try:
        with startup_claim(sys.argv[1:]) as forwarded:
            return 0 if forwarded else _run_gui()
    except Exception:
        _report(f"{DISPLAY_NAME} - startup failed", traceback.format_exc())
        return 3


def _run_gui() -> int:
    """Load dependencies only after this process wins cold-start ownership."""

    try:
        from PIL import Image  # noqa: F401  - fail early with a clear message
    except Exception:
        _report(f"{DISPLAY_NAME} - missing dependency",
                "Pillow is required.\n\nInstall it with:\n    pip install pillow\n\n"
                + traceback.format_exc())
        return 2

    try:
        from figureviewer.app import main as run
    except Exception:
        _report(f"{DISPLAY_NAME} - startup failed", traceback.format_exc())
        return 3

    try:
        return run()
    except Exception:
        _report(f"{DISPLAY_NAME} - crashed", traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
