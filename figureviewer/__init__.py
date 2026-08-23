"""Figure Viewer: a fast, themed image and PDF browser for Windows.

Package layout
--------------
``theme``    : colour palettes, ttk styling, custom themed menu bar.
``winclip``  : Win32 clipboard helpers (PNG + DIBV5 + DIB + HDROP + text).
``docs``     : file scanning, document abstraction (raster images and PDFs),
               LRU caches and background thumbnail generation.
``viewer``   : the viewer window (one per open folder) with its canvas, view
               transform, snipping tool and all commands.
``app``      : root application object, settings persistence, window registry.
"""

__all__ = ["APP_NAME", "APP_VERSION"]

APP_NAME = "Figure Viewer"
APP_VERSION = "2.2"
