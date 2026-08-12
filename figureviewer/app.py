"""Application root: settings, shared services and the window registry.

A single hidden ``tk.Tk`` acts as the Tk root; every visible window is a
``Toplevel``. That is what makes File > New Window safe: several ``tk.Tk``
instances in one process each get their own interpreter and cannot share
images, fonts or styles.

Shared across all windows:

* ``worker``      - the background thread pool (scan / decode / thumbnails);
* ``cache``       - the LRU of open documents, so two windows showing the same
                    figure decode it once;
* ``thumb_cache`` - rendered thumbnails, keyed by path + mtime + size;
* ``settings``    - persisted to ``%APPDATA%/FigureViewerApp/settings.json``.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from typing import Optional

from . import APP_NAME
from . import docs as D
from .theme import PALETTES, Palette, apply_theme, configure_fonts, enable_dpi_awareness

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_DIR = Path.home() / "AppData" / "Roaming" / "FigureViewerApp"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"

DEFAULT_SETTINGS: dict = {
    "theme": "dark",
    "geometry": "1560x960+70+50",
    "last_folder": "",
    "recent": [],
    "filter": D.FILTER_ALL,
    "sort_by": "name",
    "sort_desc": False,
    "recursive": False,
    "auto_reload": True,
    "show_sidebar": True,
    "show_info": False,
    "show_minimap": True,
    "smooth": True,
    "transparency": "white",
    "thumb_size": 40,
    "pdf_dpi": 150.0,
    "export_dpi": 300.0,
    "slideshow_secs": 4.0,
    "snip_sticky": False,
}


def load_settings() -> dict:
    """Read settings, falling back to defaults for anything missing."""
    values = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            values.update(stored)
    except Exception:
        pass
    return values


def save_settings(values: dict) -> None:
    """Persist settings, ignoring failures (a read-only profile must not crash)."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(values, indent=2), encoding="utf-8")
    except Exception:
        pass


class App:
    """Owns the Tk root and every viewer window."""

    def __init__(self, folder: Optional[Path] = None, select: Optional[Path] = None) -> None:
        self.scale = enable_dpi_awareness()
        self.app_dir = APP_DIR
        self.settings = load_settings()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        try:
            self.root.tk.call("tk", "scaling", max(self.scale, 1.0) * 1.334)
        except Exception:
            pass
        configure_fonts(self.root, self.scale)
        self.palette: Palette = PALETTES.get(self.settings.get("theme", "dark"), PALETTES["dark"])
        apply_theme(self.root, self.palette)
        self._apply_row_height()

        self.worker = D.Worker(self.root, max_workers=4)
        self.cache = D.DocumentCache(max_items=8)
        self.thumb_cache: dict[tuple, object] = {}
        self.windows: list = []
        self._cascade = 0

        start = folder or self._initial_folder()
        self.new_window(start, select)

    # ------------------------------------------------------------- helpers
    def _initial_folder(self) -> Path:
        remembered = self.settings.get("last_folder") or ""
        if remembered:
            candidate = Path(remembered)
            if candidate.is_dir():
                return candidate
        return Path.home()

    def _apply_row_height(self) -> None:
        """Keep the file-list row height in step with the thumbnail size."""
        from tkinter import ttk

        size = int(self.settings.get("thumb_size", 40))
        ttk.Style(self.root).configure("Treeview", rowheight=max(22, size + 6) if size else 22)

    def next_geometry(self) -> str:
        """Geometry for a new window, cascaded so windows do not stack exactly."""
        base = self.settings.get("geometry", DEFAULT_SETTINGS["geometry"])
        if self._cascade == 0:
            self._cascade += 1
            return base
        offset = 28 * self._cascade
        self._cascade += 1
        try:
            size, x, y = base.replace("+", " +").replace("-", " -").split()[:3]
            return f"{size}+{int(x) + offset}+{int(y) + offset}"
        except Exception:
            return base

    # ------------------------------------------------------------- windows
    def new_window(self, folder: Optional[Path] = None, select: Optional[Path] = None):
        """Open another viewer window (File > New Window)."""
        from .viewer import ViewerWindow

        if folder is None:
            folder = self.windows[-1].folder if self.windows else self._initial_folder()
        window = ViewerWindow(self, Path(folder), select)
        self.windows.append(window)
        self.push_recent(Path(folder))
        return window

    def forget_window(self, window) -> None:
        """Called by a window when it closes; quits once the last one is gone."""
        if window in self.windows:
            self.windows.remove(window)
        if not self.windows:
            self.quit_all()

    def quit_all(self) -> None:
        """Save state, tear down the worker pool and exit the main loop."""
        save_settings(self.settings)
        try:
            self.worker.shutdown()
        except Exception:
            pass
        for window in list(self.windows):
            try:
                window.destroy()
            except Exception:
                pass
        self.windows.clear()
        try:
            self.cache.clear()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # -------------------------------------------------------------- shared
    def push_recent(self, folder: Path) -> None:
        """Move a folder to the top of the recent list."""
        text = str(folder)
        recent = [item for item in self.settings.get("recent", []) if item != text]
        recent.insert(0, text)
        self.settings["recent"] = recent[:15]
        self.settings["last_folder"] = text
        self.broadcast_recent()

    def broadcast_recent(self) -> None:
        for window in self.windows:
            try:
                window._refresh_recent_menu()
            except Exception:
                pass

    def toggle_theme(self) -> None:
        """Switch between the dark and light palettes.

        ttk styles are global and menus cache their colours, so the cleanest
        way to repaint everything is to rebuild the windows in place, keeping
        each one's folder, selected file and geometry.
        """
        name = "light" if self.palette.name == "dark" else "dark"
        self.palette = PALETTES[name]
        self.settings["theme"] = name
        state = [(w.folder, w.current.path if w.current else None, w.geometry())
                 for w in list(self.windows)]
        for window in list(self.windows):
            window._destroyed = True
            try:
                window.destroy()
            except Exception:
                pass
        self.windows.clear()
        apply_theme(self.root, self.palette)
        self._apply_row_height()
        self._cascade = 0
        for folder, selected, geometry in state:
            window = self.new_window(folder, selected)
            try:
                window.geometry(geometry)
            except Exception:
                pass
        save_settings(self.settings)

    def run(self) -> None:
        self.root.mainloop()


def resolve_target(argument: Optional[str]) -> tuple[Optional[Path], Optional[Path]]:
    """Turn a command-line argument into ``(folder, file_to_select)``."""
    if not argument:
        return None, None
    path = Path(argument).expanduser()
    if path.is_file():
        return path.parent, path
    if path.is_dir():
        return path, None
    return None, None


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point used by ``figure_viewer_app.py``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    folder, select = resolve_target(argv[0] if argv else None)
    App(folder, select).run()
    return 0
