#!/usr/bin/env python3
"""Launch the unmodified FigView source with Ubuntu desktop integrations."""
from pathlib import Path
import io
import os
import shutil
import subprocess
import sys
import types

# Installation paths and desktop identity are configurable here.
APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "FigureViewerApp"
XCLIP = shutil.which("xclip") or str(Path(sys.executable).parent / "xclip")
DESKTOP_ID = "figview.desktop"
UI_FONT = "Noto Sans"
MONO_FONT = "DejaVu Sans Mono"
UI_FONT_SIZE = 10
sys.path.insert(0, str(APP_DIR))


def publish(payload, target):
    """Transfer bytes to X11's clipboard, also bridged by GNOME to Wayland."""
    # xclip forks to retain selection ownership after the launcher returns.
    result = subprocess.run([str(XCLIP), "-selection", "clipboard", "-t", target],
                            input=payload, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5)
    if result.returncode:
        raise RuntimeError("Unable to write the desktop clipboard")


def copy_text(text):
    """Copy Unicode text without using the Windows clipboard API."""
    publish(text.encode("utf-8"), "UTF8_STRING")


def copy_image(image, *, dpi=None, background="white"):
    """Copy a lossless PNG, preserving transparency and optional resolution."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", **({"dpi": dpi} if dpi else {}))
    publish(buffer.getvalue(), "image/png")


def copy_files(paths):
    """Publish GNOME's copied-file format for pasting in the file manager."""
    if paths:
        publish(("copy\n" + "\n".join(Path(p).resolve().as_uri() for p in paths)).encode(),
                "x-special/gnome-copied-files")


# Supply the platform implementation before the viewer imports its clipboard.
clipboard = types.ModuleType("figureviewer.winclip")
clipboard.ClipboardError = RuntimeError
clipboard.copy_text = copy_text
clipboard.copy_image = copy_image
clipboard.copy_files = copy_files
sys.modules[clipboard.__name__] = clipboard

from figureviewer import ipc


def set_paths(module):
    """Use the standard per-user Linux settings directory for GUI and IPC."""
    module.SETTINGS_DIR = CONFIG_DIR
    module.SETTINGS_PATH = CONFIG_DIR / "settings.json"
    module.INSTANCE_PATH = CONFIG_DIR / "instance.json"
    if hasattr(module, "STARTUP_LOCK_PATH"):
        module.STARTUP_LOCK_PATH = CONFIG_DIR / "startup.lock"


set_paths(ipc)
# Preserve inexpensive handoff to an existing window before loading GUI modules.
if sys.argv[1:2] != ["--self-test"] and ipc.forward_arguments(sys.argv[1:]):
    raise SystemExit(0)

from figureviewer import app, viewer
set_paths(app)
original_bind_canvas = viewer.ViewerWindow._bind_canvas
original_configure_fonts = app.configure_fonts


def configure_linux_fonts(root, scale=1.0):
    """Use smooth, installed Linux fonts for labels, menus, and readouts."""
    import tkinter.font as tkfont

    fonts = original_configure_fonts(root, scale)
    for key, font in fonts.items():
        font.configure(
            family=MONO_FONT if key == "mono" else UI_FONT,
            size=UI_FONT_SIZE + (3 if key == "title" else -1 if key in ("small", "mono") else 0),
            weight="bold" if key in ("ui_bold", "title") else "normal",
        )
    for name in ("TkDefaultFont", "TkMenuFont", "TkTextFont", "TkHeadingFont",
                 "TkCaptionFont", "TkSmallCaptionFont", "TkTooltipFont"):
        tkfont.nametofont(name, root=root).configure(family=UI_FONT, size=UI_FONT_SIZE)
    # Retain Font objects: Tkinter deletes named fonts when they are collected.
    root._figview_fonts = fonts
    return fonts


app.configure_fonts = configure_linux_fonts
original_open_paths = app.App.open_paths


def open_exact_folders(self, paths):
    """Limit external folder requests to the supplied folder's direct contents.

    This runs on the receiving GUI for both startup and IPC requests. Changing
    the depth before scheduling the scan prevents a previously recursive disk
    view from leaking into the newly selected folder.
    """
    targets = [Path(path).expanduser().absolute() for path in paths]
    if any(path.is_dir() for path in targets):
        self.settings["scan_depth"] = 0
        for window in self.windows:
            window.scan_depth.set(0)
    return original_open_paths(self, targets)


app.App.open_paths = open_exact_folders


def bind_canvas(self):
    """Add X11 wheel events while retaining the app's existing key bindings."""
    original_bind_canvas(self)

    def wheel(event):
        """Normalize X11 wheel buttons to the deltas expected by FigView."""
        event.delta = 120 if event.num == 4 else -120
        if event.state & 4:
            return self._on_zoom_wheel(event)
        if event.state & 1:
            self._pan_by(-event.delta * 0.6, 0)
            return "break"
        return self._on_wheel(event)

    self.canvas.bind("<Button-4>", wheel)
    self.canvas.bind("<Button-5>", wheel)


def reveal(path):
    """Open the containing directory using the user's file manager."""
    subprocess.Popen(["xdg-open", str(Path(path).parent)])


def recycle(path):
    """Move a file to the desktop trash through GIO, never unlink it."""
    subprocess.run(["gio", "trash", str(path)], check=True)


def register(self):
    """Set the installed desktop entry as the current user's PDF default."""
    subprocess.run(["xdg-mime", "default", DESKTOP_ID, "application/pdf"], check=True)
    self.toast("FigView is the default PDF reader")


viewer.ViewerWindow._bind_canvas = bind_canvas
viewer.ViewerWindow._recycle = staticmethod(recycle)
viewer.ViewerWindow.register_file_types = register
viewer._reveal_in_explorer = reveal

if __name__ == "__main__":
    from figure_viewer_app import main
    raise SystemExit(main())
