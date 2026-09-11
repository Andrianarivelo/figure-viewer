"""FigView: a fast, themed image, video, and PDF browser for Windows.

Package layout
--------------
``theme``    : colour palettes, ttk styling, custom themed menu bar.
``winclip``  : Win32 clipboard helpers (PNG + DIBV5 + DIB + HDROP + text).
``docs``     : file scanning, still-document abstraction, caches and thumbnails.
``video``    : bounded, threaded OpenCV playback for local video files.
``icons``    : DPI-aware native control icons and generated-brand loading.
``viewer``   : the viewer window (explicit files or one folder) with its canvas,
               view transform, snipping tool and all commands.
``app``      : root application object, settings persistence, window registry.
``pdfedit``  : headless PDF edit sessions: commands, undo/redo, atomic saving.
``reflow``   : in-place paragraph editing with reflow, find and replace.
``forms``    : AcroForm model, identity presets and reviewed auto-fill.
``signing``  : signature library, stamping and pyHanko certificate signing.
``ocr``      : integrated-Tesseract OCR, preprocessing, language packs.
``pdfexport``: export to Word, Excel, PowerPoint, text, HTML and Markdown.
``editui``   : the Tk edit-mode controller gluing the studio to the viewer.
``dialogs``  : themed dialogs for export, signatures, identities and OCR.
"""

__all__ = ["APP_NAME", "APP_VERSION", "VIDEO_SUFFIXES"]

APP_NAME = "FigView"
APP_VERSION = "1.1.0"

# Shared by scanning and playback. Keeping this tiny table in the package root
# avoids an additional filesystem import on the image/PDF startup path.
VIDEO_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv",
    ".mpg", ".mpeg", ".mpe", ".ogv", ".mts", ".m2ts", ".ts",
})
