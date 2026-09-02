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

``FigureViewerApp`` is intentionally retained as the private storage identity
after the public application name changed to FigView. Keeping it preserves
settings and lets old and new launchers share the same single-instance channel.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Optional

from . import APP_NAME
from . import docs as D
from .theme import (PALETTES, SIDEBAR_DEFAULT, Palette, apply_theme,
                    configure_fonts, configure_list_density, enable_dpi_awareness)

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_DIR = Path.home() / "AppData" / "Roaming" / "FigureViewerApp"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"

# Schema 2 changed folder discovery to all levels. Schema 3 separates recent
# files from recent folders. Each migration is bounded to its source schema so
# later explicit user choices remain untouched.
SETTINGS_SCHEMA_VERSION = 3

#: Where a running instance advertises the loopback port it listens on, so that
#: a second launch (a double-clicked PNG, say) can hand its file over instead of
#: starting a whole second copy of the app. See :class:`InstanceServer`.
INSTANCE_PATH = SETTINGS_DIR / "instance.json"

DEFAULT_SETTINGS: dict = {
    "settings_schema": SETTINGS_SCHEMA_VERSION,
    "theme": "dark",
    "geometry": "1560x960+70+50",
    "last_folder": "",
    "recent_files": [],
    "recent_folders": [],
    "filter": D.FILTER_ALL,
    "sort_by": "name",
    "sort_desc": False,
    # Sub-folder levels to include when listing a folder: 0 = that folder
    # only, n = n levels below it, -1 = the whole tree. See docs.scan_folder.
    "scan_depth": D.DEPTH_UNLIMITED,
    # Reuse an already-open window when a file is opened from Explorer. This is
    # what makes the app pleasant as the system default viewer: clicking ten
    # PNGs in a row fills one window instead of opening ten.
    "single_instance": True,
    "auto_reload": True,
    "show_sidebar": True,
    # Width of the file list, in pixels. Adjustable by dragging the handle
    # between the list and the canvas.
    "sidebar_width": SIDEBAR_DEFAULT,
    "show_info": False,
    "show_minimap": True,
    "smooth": True,
    "transparency": "white",
    "thumb_size": 24,
    "density": "compact",
    "pdf_dpi": 150.0,
    "export_dpi": 300.0,
    "slideshow_secs": 4.0,
    "snip_sticky": False,
    # Video starts immediately on selection by default. Loop is opt-in and is
    # also exposed directly on the transport bar.
    "video_autoplay": True,
    "video_loop": False,
    # Up/Down jump this many video frames. Left/Right always move one frame.
    "video_frame_step": 10,
}


def load_settings() -> dict:
    """Read settings, falling back to defaults for anything missing."""
    values = dict(DEFAULT_SETTINGS)
    stored: dict = {}
    try:
        # utf-8-sig, not utf-8: a settings file that has been through Notepad
        # or PowerShell's Set-Content carries a byte-order mark, and a plain
        # utf-8 read would leave it in the string, break json.loads and throw
        # every preference away without a word.
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            stored = loaded
            values.update(stored)
    except Exception:
        pass
    # Migration from the 2.0 boolean, whose "include subfolders" meant "all of
    # them". Tested against `stored`, not `values`: the merged dict always has
    # a scan_depth because the default provides one.
    if "scan_depth" not in stored and stored.get("recursive"):
        values["scan_depth"] = D.DEPTH_UNLIMITED
    # Version 3.0 stored its top-level-only default as an ordinary value, which
    # is why an apparently valid folder silently omitted deeper results. Move
    # old profiles to the safer all-level default once. A choice made after
    # this migration remains untouched because the current schema is saved.
    try:
        stored_schema = int(stored.get("settings_schema", 1) or 1)
    except (TypeError, ValueError):
        stored_schema = 1
    if stored_schema < 2:
        try:
            stored_depth = int(stored.get("scan_depth", 0))
        except (TypeError, ValueError):
            stored_depth = 0
        if stored_depth == 0:
            values["scan_depth"] = D.DEPTH_UNLIMITED
    # Schema 2 had one folder-only list named ``recent``. Preserve it under
    # the clearer split introduced for Adobe-style explicit file collections.
    if "recent_folders" not in stored and isinstance(stored.get("recent"), list):
        values["recent_folders"] = stored["recent"]
    for key in ("recent_files", "recent_folders"):
        if not isinstance(values.get(key), list):
            values[key] = []
    values["settings_schema"] = SETTINGS_SCHEMA_VERSION
    try:
        depth = int(values["scan_depth"])
        values["scan_depth"] = depth if depth >= D.DEPTH_UNLIMITED else D.DEPTH_UNLIMITED
    except (KeyError, TypeError, ValueError):
        values["scan_depth"] = DEFAULT_SETTINGS["scan_depth"]
    try:
        values["video_frame_step"] = max(1, min(10_000, int(values["video_frame_step"])))
    except (KeyError, TypeError, ValueError):
        values["video_frame_step"] = DEFAULT_SETTINGS["video_frame_step"]
    values.pop("recursive", None)
    values.pop("recent", None)
    return values


def save_settings(values: dict) -> None:
    """Persist settings, ignoring failures (a read-only profile must not crash)."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(values, indent=2), encoding="utf-8")
    except Exception:
        pass


class InstanceServer:
    """A tiny loopback server that lets a second launch reuse this window.

    Why this exists
    ---------------
    Once FigView is the system default for PNGs and PDFs, every
    double-click in Explorer starts a fresh Python process: about a second of
    interpreter start-up, a new window, and a second copy of every cached
    document. Instead, the first instance listens on an ephemeral loopback port
    and later launches simply hand it the path and exit.

    Safety
    ------
    * bound to ``127.0.0.1`` only, so nothing off this machine can reach it;
    * an ephemeral port, published in a file inside the user's own profile;
    * a random per-run token that the client must echo back, so another local
      process cannot drive the viewer just by guessing the port;
    * the payload is a JSON list of paths and nothing else - it is never
      executed, only opened.

    Failure is always non-fatal: if anything here breaks, the app simply keeps
    behaving the way it did before, one process per launch.
    """

    def __init__(self, app: "App") -> None:
        self.app = app
        self.token = secrets.token_hex(16)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self._publish()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _publish(self) -> None:
        """Advertise the port and token for the next launch to find."""
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        INSTANCE_PATH.write_text(
            json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}),
            encoding="utf-8")

    def _serve(self) -> None:
        """Accept handoffs until the socket is closed at shutdown."""
        while True:
            try:
                conn, _addr = self.socket.accept()
            except OSError:
                return                      # socket closed: the app is quitting
            paths: list[Path] = []
            with conn:
                try:
                    conn.settimeout(2.0)
                    raw = conn.recv(64 * 1024).decode("utf-8", "replace")
                    message = json.loads(raw)
                    if message.get("token") != self.token:
                        continue
                    paths = [Path(item) for item in message.get("paths", [])
                             if isinstance(item, str)]
                    conn.sendall(b'{"ok": true}')
                except Exception:
                    continue
            if paths:
                # Tk is not thread-safe: hop back onto the UI thread.
                self.app.root.after(0, lambda p=paths: self.app.open_paths(p))

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass
        try:
            if INSTANCE_PATH.exists():
                stored = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
                if stored.get("pid") == os.getpid():
                    INSTANCE_PATH.unlink()
        except Exception:
            pass


def hand_over_to_running_instance(paths: list[Path]) -> bool:
    """Give ``paths`` to an already-running viewer. True if it accepted them.

    A stale instance file - left behind by a crash - simply fails to connect,
    and the caller starts a normal instance which overwrites it.
    """
    try:
        stored = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        port, token = int(stored["port"]), str(stored["token"])
    except Exception:
        return False
    payload = json.dumps({"token": token, "paths": [str(p) for p in paths]})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as conn:
            conn.settimeout(1.5)
            conn.sendall(payload.encode("utf-8"))
            return b'"ok"' in conn.recv(1024)
    except Exception:
        return False


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

        # A remembered directory is only a dialog starting point. It must not
        # silently become an active folder listing: folders open only after an
        # explicit directory target or an Open Folder action.
        start = folder or self._initial_folder()
        if select is not None:
            self.new_window(start, select, explicit_paths=[select])
        elif folder is not None:
            self.new_window(start)
        else:
            self.new_window(start, explicit_paths=[])

        self.instance: Optional[InstanceServer] = None
        if self.settings.get("single_instance", True):
            try:
                self.instance = InstanceServer(self)
            except Exception:
                self.instance = None      # never let IPC stop the app starting

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
        configure_list_density(self.root, int(self.settings.get("thumb_size", 24)),
                               self.settings.get("density", "compact"))

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
    def new_window(self, folder: Optional[Path] = None, select: Optional[Path] = None,
                   *, explicit_paths: Optional[list[Path]] = None):
        """Open another viewer window (File > New Window)."""
        from .viewer import ViewerWindow

        if folder is None:
            folder = self._initial_folder()
            if explicit_paths is None and select is None:
                explicit_paths = []
        window = ViewerWindow(self, Path(folder), select, explicit_paths=explicit_paths)
        self.windows.append(window)
        if explicit_paths is None:
            self.push_recent_folder(Path(folder))
        for path in explicit_paths or []:
            self.push_recent_file(path)
        return window

    def open_paths(self, paths: list[Path]) -> None:
        """Show files or folders handed over by a second launch.

        Files append to the most recently used window's explicit list. A
        directory remains an explicit request to switch that window to folder
        mode. This mirrors document readers: opening another document adds it
        to the session instead of importing every sibling from disk.
        """
        window = self.windows[-1] if self.windows else None
        for raw in paths:
            folder, select = resolve_target(str(raw))
            if folder is None:
                continue
            if window is None:
                window = self.new_window(
                    folder, select, explicit_paths=[select] if select else None)
            else:
                window.reveal_path(select or folder)
            self.focus_window(window)

    def focus_window(self, window) -> None:
        """Bring a window to the front, the way a shell-opened file should.

        Windows refuses focus to a process the user did not just interact with,
        so the usual lift/focus pair is not enough; a brief topmost flip is the
        standard way round it without leaving the window permanently on top.
        """
        def drop_topmost() -> None:
            try:
                window.attributes("-topmost", False)
            except Exception:
                pass          # the window may have been closed in the meantime

        try:
            window.deiconify()
            window.lift()
            window.attributes("-topmost", True)
            window.after(180, drop_topmost)
            window.focus_force()
        except Exception:
            pass

    def forget_window(self, window) -> None:
        """Called by a window when it closes; quits once the last one is gone."""
        if window in self.windows:
            self.windows.remove(window)
        if not self.windows:
            self.quit_all()

    def quit_all(self) -> None:
        """Save state, tear down the worker pool and exit the main loop."""
        save_settings(self.settings)
        if getattr(self, "instance", None) is not None:
            self.instance.close()
        try:
            self.worker.shutdown()
        except Exception:
            pass
        for window in list(self.windows):
            try:
                window._stop_video_session()
            except Exception:
                pass
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
    def _push_recent(self, key: str, path: Path) -> None:
        """Move a path to the top of one bounded recent-items list."""
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        text = str(path)
        recent = [item for item in self.settings.get(key, []) if item != text]
        recent.insert(0, text)
        self.settings[key] = recent[:15]
        self.broadcast_recent()

    def push_recent_file(self, path: Path) -> None:
        self._push_recent("recent_files", path)

    def push_recent_folder(self, folder: Path) -> None:
        self._push_recent("recent_folders", folder)
        self.settings["last_folder"] = str(folder)

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
        state = [(w.folder, w.current.path if w.current else None,
                  list(w.explicit_paths) if w.explicit_paths is not None else None,
                  w.geometry())
                 for w in list(self.windows)]
        for window in list(self.windows):
            window._destroyed = True
            try:
                window._stop_video_session()
            except Exception:
                pass
            try:
                window.destroy()
            except Exception:
                pass
        self.windows.clear()
        apply_theme(self.root, self.palette)
        self._apply_row_height()
        self._cascade = 0
        for folder, selected, explicit_paths, geometry in state:
            window = self.new_window(folder, selected, explicit_paths=explicit_paths)
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
    """Entry point used by ``figure_viewer_app.py``.

    Accepts any number of files or folders, which is what Explorer sends when
    several selected files are opened at once. If a viewer is already running
    and single-instance mode is on, the paths are handed to it and this process
    exits immediately instead of starting a duplicate.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    targets = [Path(item).expanduser() for item in argv if item and not item.startswith("-")]

    if targets and load_settings().get("single_instance", True):
        if hand_over_to_running_instance(targets):
            return 0

    folder, select = resolve_target(str(targets[0]) if targets else None)
    app = App(folder, select)
    if len(targets) > 1:
        app.root.after(400, lambda: app.open_paths(targets[1:]))
    app.run()
    return 0
