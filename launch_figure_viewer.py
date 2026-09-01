"""Robust backward-compatible launcher for FigView.

The shortcut points here instead of directly to the GUI script so startup
failures are written to a log file. This matters when `pythonw.exe` is used:
without a console, import errors or bad paths otherwise look like "nothing
happened".

Interpreter selection
---------------------
This file used to hardcode ``C:\\Users\\bellone\\anaconda3\\python.exe``. That
path exists on exactly one machine and one user account, so on every other one
the launcher silently fell back to whatever interpreter happened to start it.
The resolver below is explicit instead, and it *checks that the interpreter can
actually import Pillow* before choosing it: several Python installations are on
PATH here and only some of them have the GUI's dependencies.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Iterator, Optional


APP_DIR = Path(__file__).resolve().parent
APP_SCRIPT = APP_DIR / "figure_viewer_app.py"
LOG_PATH = APP_DIR / "figure_viewer_launcher.log"

#: Public override plus the historical name retained for compatibility.
ENV_OVERRIDE = "FIGVIEW_PYTHON"
LEGACY_ENV_OVERRIDE = "FIGURE_VIEWER_PYTHON"


def _candidates() -> Iterator[Path]:
    """Interpreters worth trying, best first.

    The interpreter that started this launcher comes first: it is whatever the
    shortcut selected, so respecting it keeps an explicitly configured setup
    working. Common per-user conda installs follow, then anything on PATH.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        yield Path(override)
    legacy_override = os.environ.get(LEGACY_ENV_OVERRIDE, "").strip()
    if legacy_override:
        yield Path(legacy_override)

    if sys.executable:
        yield Path(sys.executable)

    home = Path.home()
    for root in (home / "miniconda3", home / "anaconda3",
                 home / "AppData" / "Local" / "miniconda3",
                 home / "AppData" / "Local" / "anaconda3"):
        yield root / "python.exe"

    for name in ("python.exe", "pythonw.exe", "python3.exe"):
        found = _which(name)
        if found is not None:
            yield found


def _which(name: str) -> Optional[Path]:
    """First ``name`` on PATH, or None."""
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        cand = Path(part) / name
        if cand.is_file():
            return cand
    return None


def _console_variant(exe: Path) -> Path:
    """The python.exe next to a pythonw.exe, for probing with an exit code."""
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.is_file():
            return sibling
    return exe


def _windowless_variant(exe: Path) -> Path:
    """The pythonw.exe next to a python.exe, so no console window flashes."""
    if exe.name.lower() == "python.exe":
        sibling = exe.with_name("pythonw.exe")
        if sibling.is_file():
            return sibling
    return exe


def _can_run_gui(exe: Path) -> bool:
    """True if this interpreter can import the GUI's hard dependencies."""
    probe = _console_variant(exe)
    if not probe.is_file():
        return False
    try:
        done = subprocess.run(
            [str(probe), "-c", "import PIL, tkinter"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        return False
    return done.returncode == 0


def resolve_python(log=None) -> Path:
    """Pick an interpreter that can actually run the viewer.

    Falls back to the first interpreter that merely exists if none of them can
    import Pillow, so that the GUI still starts and shows its own "Pillow is
    required" message box rather than failing with nothing on screen.
    """
    seen, first_existing = set(), None
    for cand in _candidates():
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if cand in seen or not cand.is_file():
            continue
        seen.add(cand)
        if first_existing is None:
            first_existing = cand
        if _can_run_gui(cand):
            return _windowless_variant(cand)
        if log is not None:
            log.write(f"  skipped (no PIL/tkinter): {cand}\n")
    if first_existing is None:
        raise FileNotFoundError("No usable Python interpreter found.")
    return _windowless_variant(first_existing)


def main() -> int:
    """Start the GUI with a working interpreter and log any failure."""
    try:
        if not APP_SCRIPT.exists():
            raise FileNotFoundError(f"Missing GUI script: {APP_SCRIPT}")
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\n--- launch from {APP_DIR}\n")
            python = resolve_python(log)
            cmd = [str(python), str(APP_SCRIPT), *sys.argv[1:]]
            log.write(f"Launching: {' '.join(cmd)}\n")
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
