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
import queue
import secrets
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Optional

from . import APP_NAME
from . import docs as D
from . import ipc
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
HANDOFF_POLL_MS = 40
HANDOFF_BATCH_LIMIT = 256

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
    # Optional reading mode: reveal the file list on demand, then reclaim its
    # width after five seconds. The delay is fixed so the toolbar wording and
    # behavior cannot drift apart.
    "sidebar_auto_hide": False,
    # Width of the file list, in pixels. Adjustable by dragging the handle
    # between the list and the canvas.
    "sidebar_width": SIDEBAR_DEFAULT,
    "show_info": False,
    "show_minimap": True,
    "smooth": True,
    "transparency": "white",
    "open_fit": "optimal",
    "dock_width": 330,
    "thumb_size": 24,
    "density": "normal",
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
    # ------------------------------------------------------------------ PDF
    # studio (3.5): editing, signatures, identities, export, OCR. All pure
    # additions, so the schema version stays at 3: load_settings simply merges
    # missing keys from these defaults.
    "annot_defaults": {
        "highlight_color": "#ffd54a", "markup_opacity": 0.35,
        "ink_color": "#d84343", "ink_width": 1.6,
        "shape_stroke": "#d84343", "shape_fill": "", "shape_width": 1.4,
        "text_font": "helv", "text_size": 11.0, "text_color": "#1a1a1a",
        "note_icon": "Comment",
    },
    "edit_recent_colors": [],
    # Signature metadata only; the PNG strokes live as files under
    # SETTINGS_DIR/signatures so settings.json stays small.
    "signature_library": [],
    "active_signature": "",
    "identity_presets": [],
    "active_identity": "",
    "export_prefs": {
        "last_format": "docx", "range": "all", "image_format": "png",
        "pptx_dpi": 150, "ocr_missing_text": True,
    },
    # Path only. Certificate passphrases are collected per signing action and
    # are never persisted anywhere.
    "sign_certificate_path": "",
    "ocr_language": "eng",
    "ocr_variant": "fast",
    "ocr_preprocess": True,
    "ocr_dpi": 300,
    # Lowercased embedded font base name -> CSS family, merged over the
    # built-in table in figureviewer.reflow.
    "reflow_font_overrides": {},
    # While edit mode is active a plain wheel scrolls/pans instead of turning
    # pages, so a drawing gesture can never flip the page underneath itself.
    "edit_wheel_scrolls": True,
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
    # PDF-studio keys: hand-edited JSON must never crash the app, so every
    # structured value is type-checked and falls back to its default, and the
    # dict-shaped ones are merged so a missing sub-key cannot KeyError later.
    for key in ("signature_library", "identity_presets", "edit_recent_colors"):
        if not isinstance(values.get(key), list):
            values[key] = list(DEFAULT_SETTINGS[key])
    for key in ("annot_defaults", "export_prefs", "reflow_font_overrides"):
        merged = dict(DEFAULT_SETTINGS[key])
        if isinstance(values.get(key), dict):
            merged.update(values[key])
        values[key] = merged
    for key in ("active_signature", "active_identity", "sign_certificate_path",
                "ocr_language", "ocr_variant"):
        if not isinstance(values.get(key), str):
            values[key] = DEFAULT_SETTINGS[key]
    if values["ocr_variant"] not in ("fast", "best"):
        values["ocr_variant"] = DEFAULT_SETTINGS["ocr_variant"]
    if values.get("open_fit") not in ("optimal", "width", "page", "reading"):
        values["open_fit"] = DEFAULT_SETTINGS["open_fit"]
    try:
        values["dock_width"] = max(220, min(760, int(values["dock_width"])))
    except (KeyError, TypeError, ValueError):
        values["dock_width"] = DEFAULT_SETTINGS["dock_width"]
    try:
        values["ocr_dpi"] = max(150, min(600, int(values["ocr_dpi"])))
    except (KeyError, TypeError, ValueError):
        values["ocr_dpi"] = DEFAULT_SETTINGS["ocr_dpi"]
    values["ocr_preprocess"] = bool(values.get("ocr_preprocess", True))
    values["edit_wheel_scrolls"] = bool(values.get("edit_wheel_scrolls", True))
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
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self._pending: queue.Queue = queue.Queue()
        self._closed = False
        self._poll_job = app.root.after(HANDOFF_POLL_MS, self._drain)
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
                    message = ipc.read_message(conn)
                    if message.get("token") != self.token:
                        continue
                    items = message.get("paths", [])
                    if not isinstance(items, list):
                        continue
                    paths = [Path(item) for item in items
                             if isinstance(item, str)]
                    if paths:
                        kind = "combine" if message.get("combine") is True else "open"
                        self._pending.put((kind, paths))
                    conn.sendall(b'{"ok": true}\n')
                except Exception:
                    continue
    def _drain(self) -> None:
        """Combine Explorer launches on the UI thread without worker Tk calls."""
        if self._closed:
            return
        paths: list[Path] = []
        combine: list[Path] = []
        try:
            for _ in range(HANDOFF_BATCH_LIMIT):
                try:
                    kind, items = self._pending.get_nowait()
                except queue.Empty:
                    break
                (combine if kind == "combine" else paths).extend(items)
            if paths:
                self.app.open_paths(paths)
            if combine:
                self.app.combine_request(combine)
        finally:
            if not self._closed:
                self._poll_job = self.app.root.after(HANDOFF_POLL_MS, self._drain)

    def close(self) -> None:
        self._closed = True
        try:
            self.app.root.after_cancel(self._poll_job)
        except Exception:
            pass
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


def hand_over_to_running_instance(paths: list[Path], *, combine: bool = False) -> bool:
    """Give ``paths`` to an already-running viewer. True if it accepted them.

    A stale instance file - left behind by a crash - simply fails to connect,
    and the caller starts a normal instance which overwrites it.
    """
    return ipc.hand_over_to_running_instance(paths, instance_path=INSTANCE_PATH,
                                             combine=combine)


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
        # PDF edit sessions, keyed by normalised absolute path. App-level so
        # they survive the theme toggle's window rebuild, and one-per-file so
        # two windows can never hold divergent unsaved edits of one document.
        self.edit_sessions: dict[str, object] = {}
        self._session_owners: dict[str, object] = {}
        # Explorer's "Combine with FigView" starts one process per selected
        # PDF; their paths gather here until the launches stop arriving.
        self._combine_pending: list[Path] = []
        self._combine_job: Optional[str] = None
        self.combine_dialog = None

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
        self.push_recent_files(explicit_paths or [])
        return window

    def open_paths(self, paths: list[Path]) -> None:
        """Show files or folders handed over by a second launch.

        Files append to the most recently used window's explicit list. A
        directory remains an explicit request to switch that window to folder
        mode. This mirrors document readers: opening another document adds it
        to the session instead of importing every sibling from disk.
        """
        window = self.windows[-1] if self.windows else None
        files: list[Path] = []

        def flush_files() -> None:
            """Apply one file collection, with one scan and one selection."""
            nonlocal window
            if not files:
                return
            if window is None:
                window = self.new_window(files[-1].parent, files[-1],
                                         explicit_paths=list(files))
            else:
                window.add_files(list(files))
            files.clear()

        for raw in paths:
            folder, select = resolve_target(str(raw))
            if folder is None:
                continue
            if select is not None:
                files.append(select)
                continue
            # A folder is an ordering boundary: preserve mixed file/folder
            # requests while combining consecutive files into one operation.
            flush_files()
            if window is None:
                window = self.new_window(folder)
            else:
                window.reveal_path(folder)
        flush_files()
        if window is not None:
            self.focus_window(window)

    # ------------------------------------------------------------- combine
    def combine_request(self, paths: list[Path]) -> None:
        """Collect PDFs from ``--combine`` launches; the dialog opens once
        no new launch has arrived for :data:`combine.SHELL_SETTLE_MS`."""
        from . import combine as C
        fresh = [path for path in paths if C.is_pdf(path) and path.is_file()]
        self._combine_pending.extend(fresh)
        if self._combine_job is not None:
            try:
                self.root.after_cancel(self._combine_job)
            except Exception:
                pass
        self._combine_job = self.root.after(C.SHELL_SETTLE_MS, self._flush_combine)

    def _flush_combine(self) -> None:
        self._combine_job = None
        paths = list(self._combine_pending)
        self._combine_pending.clear()
        if not paths:
            return
        dialog = self.combine_dialog
        if dialog is not None and dialog.winfo_exists():
            # Late arrivals join the open dialog, still in Explorer order.
            dialog.add_paths(paths, alphabetical=True)
            try:
                dialog.lift()
                dialog.focus_force()
            except Exception:
                pass
            return
        window = self.windows[-1] if self.windows else self.new_window()
        self.focus_window(window)
        window.combine_pdfs(paths, alphabetical=True)

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
        for session in list(self.edit_sessions.values()):
            try:
                session.close()
            except Exception:
                pass
        self.edit_sessions.clear()
        self._session_owners.clear()
        if getattr(self, "instance", None) is not None:
            self.instance.close()
        try:
            self.worker.shutdown()
        except Exception:
            pass
        for window in list(self.windows):
            window._destroyed = True
            try:
                window._stop_video_session()
            except Exception:
                pass
            try:
                window._cancel_scheduled_jobs()
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

    def push_recent_files(self, paths: list[Path]) -> None:
        """Update a whole batch with one menu refresh and bounded path work."""
        if not paths:
            return
        recent: list[str] = []
        seen: set[str] = set()
        # Only the last fifteen distinct files can survive in the menu.
        for path in reversed(paths):
            try:
                text = str(path.expanduser().resolve())
            except OSError:
                text = str(path)
            identity = os.path.normcase(text)
            if identity not in seen:
                recent.append(text)
                seen.add(identity)
            if len(recent) == 15:
                break
        for text in self.settings.get("recent_files", []):
            if os.path.normcase(text) not in seen:
                recent.append(text)
                seen.add(os.path.normcase(text))
        self.settings["recent_files"] = recent[:15]
        self.broadcast_recent()

    def push_recent_folder(self, folder: Path) -> None:
        self._push_recent("recent_folders", folder)
        self.settings["last_folder"] = str(folder)

    def broadcast_recent(self) -> None:
        for window in self.windows:
            try:
                window._refresh_recent_menu()
            except Exception:
                pass

    # -------------------------------------------------------- edit sessions
    @staticmethod
    def session_key(path: Path) -> str:
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        return os.path.normcase(str(path))

    def acquire_session(self, window, entry, *, base_dpi: float):
        """Return ``(session, owned)`` for one PDF file.

        The first window to ask becomes the owner; any other window asking
        while the owner lives gets ``owned=False`` and must stay read-only.
        A session whose owner window has closed (or was rebuilt by the theme
        toggle) is adopted by the asker with its history and dirty state
        intact.
        """
        from . import pdfedit

        key = self.session_key(entry.path)
        session = self.edit_sessions.get(key)
        owner = self._session_owners.get(key)
        owner_alive = owner is not None and owner in self.windows and owner is not window
        if session is not None and owner_alive:
            return session, False
        if session is None:
            session = pdfedit.EditSession(entry, base_dpi=base_dpi)
            self.edit_sessions[key] = session
        self._session_owners[key] = window
        return session, True

    def peek_session(self, path: Path):
        """The session for ``path`` if one exists, without creating one."""
        return self.edit_sessions.get(self.session_key(path))

    def release_session(self, path: Path, *, close: bool = False) -> None:
        """Detach the owner; with ``close`` also discard the session."""
        key = self.session_key(path)
        self._session_owners.pop(key, None)
        if close:
            session = self.edit_sessions.pop(key, None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def dirty_sessions(self) -> list:
        return [s for s in self.edit_sessions.values() if getattr(s, "dirty", False)]

    def broadcast_signatures(self) -> None:
        for window in self.windows:
            try:
                window._refresh_signature_ui()
            except Exception:
                pass

    def broadcast_identities(self) -> None:
        for window in self.windows:
            try:
                window._refresh_identity_ui()
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
        # Edit sessions are app-level and survive the rebuild; their owner
        # windows do not. The rebuilt windows re-adopt sessions by path when
        # they reload their documents.
        self._session_owners.clear()
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
    combine = ipc.COMBINE_FLAG in argv

    if targets and load_settings().get("single_instance", True):
        if hand_over_to_running_instance(targets, combine=combine):
            return 0

    # Construct the shell once, then process the entire batch together as soon
    # as Tk can paint it. No arbitrary startup delay or per-file rescanning.
    app = App()
    if targets and combine:
        app.root.after_idle(lambda: app.combine_request(targets))
    elif targets:
        app.root.after_idle(lambda: app.open_paths(targets))
    app.run()
    return 0
