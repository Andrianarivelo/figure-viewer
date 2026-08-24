"""Figure Viewer: a fast, themed image and PDF browser for Windows.

Package layout
--------------
``theme``    : colour palettes, ttk styling, custom themed menu bar.
``winclip``  : Win32 clipboard helpers (PNG + DIBV5 + DIB + HDROP + text).
``docs``     : file scanning, still-document abstraction, caches and thumbnails.
``video``    : bounded, threaded OpenCV playback for local video files.
``icons``    : DPI-aware native control icons and generated-brand loading.
``viewer``   : the viewer window (one per open folder) with its canvas, view
               transform, snipping tool and all commands.
``app``      : root application object, settings persistence, window registry.
"""

__all__ = ["APP_NAME", "APP_VERSION", "VIDEO_SUFFIXES"]

APP_NAME = "Figure Viewer"
APP_VERSION = "3.1"

# Shared by scanning and playback. Keeping this tiny table in the package root
# avoids an additional filesystem import on the image/PDF startup path.
VIDEO_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv",
    ".mpg", ".mpeg", ".mpe", ".ogv", ".mts", ".m2ts", ".ts",
})
