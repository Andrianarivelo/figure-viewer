"""Robust launcher for Figure Viewer.

The shortcut points here instead of directly to the GUI script so startup
failures are written to a log file. This matters when `pythonw.exe` is used:
without a console, import errors or bad paths otherwise look like "nothing
happened".
"""
from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
APP_SCRIPT = APP_DIR / "figure_viewer_app.py"
LOG_PATH = APP_DIR / "figure_viewer_launcher.log"
PYTHON_EXE = Path(r"C:\Users\bellone\anaconda3\python.exe")


def main() -> int:
    """Start the GUI with the current interpreter and log any failure."""
    try:
        if not APP_SCRIPT.exists():
            raise FileNotFoundError(f"Missing GUI script: {APP_SCRIPT}")
        python = PYTHON_EXE if PYTHON_EXE.exists() else Path(sys.executable)
        cmd = [str(python), str(APP_SCRIPT), *sys.argv[1:]]
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\nLaunching: {' '.join(cmd)}\n")
            log.flush()
            subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
            )
        return 0
    except Exception:
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
