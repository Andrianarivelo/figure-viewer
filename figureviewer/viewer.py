"""The FigView window.

One :class:`ViewerWindow` owns either an explicit document list or one
explicitly selected folder. Several windows can be open at the same time
(File > New Window) and they share the worker pool, document cache, and
settings file through the parent :class:`~figureviewer.app.App`.

The rendering model
-------------------
The canvas never holds a scaled copy of the whole image. Instead the window
keeps a *view transform*::

    canvas_point = (logical_point - offset) * scale

and on every repaint it asks the document to rasterise **only the region that
is actually visible**, at exactly the on-screen size. Consequences:

* zooming to 3200 % on a 12000 x 8000 px figure costs the same as 100 %;
* PDFs are re-rasterised at the current zoom, so they never look pixelated;
* memory use is bounded by the window size, not by the file size.

Repaints happen twice: an immediate low-quality pass (bilinear, and for PDFs a
reduced-resolution render) so dragging and zooming stay at interactive frame
rates, followed - about a tenth of a second after the gesture stops - by a
high-quality pass computed on a worker thread.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import time
import tkinter as tk
import webbrowser
from functools import lru_cache
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageOps, ImageTk

from . import APP_NAME, APP_VERSION
from . import docs as D
from . import video as V
from . import winclip
from .editui import EditController
from .icons import IconFactory
from .indexui import IndexController
from .linkui import LinkController
from .textui import TextController
from .theme import (DOCK_DEFAULT, DOCK_MAX, DOCK_MIN, LIST_FLAT, LIST_TREE,
                    SIDEBAR_DEFAULT, SIDEBAR_MAX, SIDEBAR_MIN, MenuBar, Palette,
                    bind_tooltip, configure_list_density, style_menu,
                    toolbar_button)

ZOOM_MIN = 0.02
ZOOM_MAX = 32.0
ZOOM_STEP = 1.25
QUALITY_DELAY_MS = 110      # idle time before the high-quality repaint
WATCH_MS = 1200             # folder polling interval
TOAST_MS = 2400
MINIMAP_MAX = 190
#: The overview map fades this long after the view last moved.
MINIMAP_FADE_MS = 1800
VIDEO_FRAME_STEP_MIN = 1
VIDEO_FRAME_STEP_MAX = 10_000
SIDEBAR_AUTO_HIDE_MS = 5_000

# PDF zoom is expressed like a desktop reader: 100% means 96 screen pixels per
# physical inch, independently of the higher DPI used internally to rasterise
# text. A portrait A4 starts at 125%; wider paper is reduced automatically so
# documents have a stable visual reading width.
PDF_SCREEN_DPI = 96.0
PDF_DEFAULT_ZOOM = 1.25
PDF_A4_WIDTH_POINTS = 595.28
PDF_MAX_VIEWPORT_WIDTH = 1.35
PDF_WHEEL_COOLDOWN_S = 0.24

#: Below this width the sidebar header has no room for the long count, and the
#: text would run under the FILES label and off the edge.
COUNT_COMPACT_BELOW = 280

#: iid prefix for the sub-folder nodes of a grouped listing. A NUL byte cannot
#: appear in a Windows path, so these can never be mistaken for a file row.
GROUP_PREFIX = "\x00dir:"

FIT_PAGE = "page"
FIT_WIDTH = "width"
FIT_READING = "reading"
FIT_OPTIMAL = "optimal"
FIT_FREE = "free"
#: Fit modes that follow the window when it is resized.
FIT_MODES_TRACKING = (FIT_PAGE, FIT_WIDTH, FIT_READING, FIT_OPTIMAL)
#: View > Zoom on Open choices: setting value -> (label, PDF mode, image mode).
OPEN_FIT_CHOICES = (
    ("optimal", "Optimal (fill the width)", FIT_OPTIMAL, FIT_OPTIMAL),
    ("width", "Fit width", FIT_WIDTH, FIT_WIDTH),
    ("page", "Fit page", FIT_PAGE, FIT_PAGE),
    ("reading", "Reading width (A4 at 125 %)", FIT_READING, FIT_PAGE),
)
#: Optimal zoom caps: a PDF page never opens above this displayed zoom, an
#: image never above this multiple of its native pixels.
PDF_OPTIMAL_MAX_ZOOM = 2.0
IMAGE_OPTIMAL_MAX_SCALE = 2.0

#: Tools > Subfolder Depth. The value is the ``max_depth`` handed to
#: :func:`figureviewer.docs.scan_folder`: 0 is the folder itself, n descends n
#: levels, -1 walks the whole tree.
DEPTH_CHOICES: tuple[tuple[str, int], ...] = (
    ("This Folder Only", 0),
    ("+ 1 Level", 1),
    ("+ 2 Levels", 2),
    ("+ 3 Levels", 3),
    ("+ 5 Levels", 5),
    ("All Levels", D.DEPTH_UNLIMITED),
)


def depth_label(value: int) -> str:
    """Human-readable name for a scan depth, for status bars and tooltips."""
    for label, candidate in DEPTH_CHOICES:
        if candidate == value:
            return label
    return f"+ {value} Levels" if value > 0 else "All Levels"


def optimal_scale(page_width: int, canvas_width: int, max_scale: float,
                  margin: int = 16) -> float:
    """Zoom that fills the canvas width, capped at ``max_scale``.

    This is what people mean by "the right size on open": a landscape scan
    in a wide window fills it instead of sitting at a fixed reading width,
    while a portrait page on an ultra-wide screen stops at the cap rather
    than growing past legibility. Height is never a constraint: a page
    taller than the viewport scrolls, as in every reader.
    """
    page_width = max(1, int(page_width))
    fill = max(1, canvas_width - margin) / page_width
    return min(max(min(fill, max_scale), ZOOM_MIN), ZOOM_MAX)


def pdf_reading_scale(page_width: int, base_dpi: float, canvas_width: int) -> float:
    """Format-aware initial PDF scale, equivalent to 125% for portrait A4.

    The target is a constant physical reading width. A3, landscape, poster,
    and custom pages therefore start lower; A5 starts higher. On an unusually
    narrow canvas the page may exceed the viewport, but never by more than 35%,
    leaving it readable and still easy to pan.
    """
    page_width = max(1, int(page_width))
    base_dpi = max(1.0, float(base_dpi))
    target_pixels = PDF_A4_WIDTH_POINTS / 72.0 * PDF_SCREEN_DPI * PDF_DEFAULT_ZOOM
    desired = target_pixels / page_width
    viewport_limit = max(1, canvas_width - 24) * PDF_MAX_VIEWPORT_WIDTH / page_width
    return min(max(min(desired, viewport_limit), ZOOM_MIN), ZOOM_MAX)


def _open_in_shell(path: Path) -> None:
    """Open a file with its default Windows application."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        webbrowser.open(path.as_uri())


def _reveal_in_explorer(path: Path) -> None:
    """Select a file in a new Explorer window."""
    subprocess.Popen(["explorer", "/select,", str(path)])


@lru_cache(maxsize=4)
def _checker_block(square: int, light: str, dark: str) -> Image.Image:
    """A pre-tiled checkerboard block, built once and reused.

    Tiling a 1500 x 900 view from a 20 px tile would need ~3 400 pastes per
    repaint; from this 512 px block it needs six.
    """
    tile = Image.new("RGB", (square * 2, square * 2), light)
    dark_square = Image.new("RGB", (square, square), dark)
    tile.paste(dark_square, (square, 0))
    tile.paste(dark_square, (0, square))
    block = Image.new("RGB", (512, 512))
    for y in range(0, 512, tile.height):
        for x in range(0, 512, tile.width):
            block.paste(tile, (x, y))
    return block


def _checker_background(size: tuple[int, int]) -> Image.Image:
    """Checkerboard backdrop of an arbitrary size."""
    block = _checker_block(10, "#ffffff", "#d3d7dd")
    background = Image.new("RGB", size)
    for y in range(0, size[1], block.height):
        for x in range(0, size[0], block.width):
            background.paste(block, (x, y))
    return background


def _mix(colour_a: str, colour_b: str, weight: float) -> str:
    """Blend two ``#rrggbb`` colours; ``weight`` = share of ``colour_a``."""
    a = tuple(int(colour_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(colour_b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(x * weight + y * (1 - weight))))) for x, y in zip(a, b))


class ViewerWindow(tk.Toplevel):
    """A single browsing window: sidebar, canvas, toolbar, status bar."""

    # ------------------------------------------------------------------ setup
    def __init__(self, app, folder: Path, select: Optional[Path] = None,
                 *, explicit_paths: Optional[list[Path]] = None) -> None:
        super().__init__(app.root)
        self.app = app
        self.palette: Palette = app.palette
        self.icons = IconFactory(self, self.palette, app.scale)
        self.folder = folder
        self.pending_select = select
        # None means an explicitly opened folder. A list means document mode,
        # including an empty new window. Opening more files appends here and
        # never imports their siblings.
        self.explicit_paths = (None if explicit_paths is None else
                               list(dict.fromkeys(Path(p) for p in explicit_paths)))

        self.entries: list[D.Entry] = []
        self.filtered: list[D.Entry] = []
        self.current: Optional[D.Entry] = None
        self.document: Optional[D.Document] = None
        self.page = 0

        # View transform -------------------------------------------------
        self.scale = 1.0
        self.off_x = 0.0
        self.off_y = 0.0
        self.fit_mode = FIT_PAGE
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False

        # Housekeeping ---------------------------------------------------
        self._generation = 0
        self._quality_job: Optional[str] = None
        self._watch_job: Optional[str] = None
        self._toast_job: Optional[str] = None
        self._slideshow_job: Optional[str] = None
        self._resize_job: Optional[str] = None
        self._video_job: Optional[str] = None
        self._sidebar_hide_job: Optional[str] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._video_photo: Optional[ImageTk.PhotoImage] = None
        self._minimap_photo: Optional[ImageTk.PhotoImage] = None
        self._minimap_key: Optional[tuple] = None
        self._minimap_rect: Optional[tuple[float, float, float, float]] = None
        self._thumb_photos: dict[str, ImageTk.PhotoImage] = {}
        self._thumb_keys: dict[str, tuple] = {}
        self._thumb_cache: dict[tuple, Image.Image] = app.thumb_cache
        self._thumb_generation = 0
        self._thumb_jobs = []
        self._load_future = None
        self._displayed: list[str] = []
        self._pan_anchor: Optional[tuple[float, float, float, float]] = None
        self._snip_start: Optional[tuple[float, float]] = None
        self._snip_rect: Optional[tuple[float, float, float, float]] = None
        self.last_snip: Optional[Image.Image] = None
        # Folder listings are produced off-thread, so results can come back out
        # of order. Every scan takes a sequence number and a result older than
        # the last one applied is dropped.
        self._list_seq = 0
        self._list_applied = 0
        self._rescan_pending = 0
        self._watch_inflight = False
        self._destroyed = False
        self._suppress_select = False
        self._probe_text = ""
        self._scan_truncated = False
        self._grouped = False
        #: Sub-folders the user collapsed, kept so a rescan - which happens
        #: every time a figure is overwritten - does not spring them open again.
        self._collapsed: set[str] = set()
        self._sash_origin: Optional[tuple[int, int]] = None
        self._video_session: Optional[V.VideoSession] = None
        self._video_scrubbing = False
        self._video_last_state = ""
        self._last_pdf_page_at = 0.0
        self._sidebar_px = max(SIDEBAR_MIN, min(SIDEBAR_MAX,
                               int(app.settings.get("sidebar_width", SIDEBAR_DEFAULT))))
        self._last_depth = int(app.settings.get("scan_depth", 0)) or D.DEPTH_UNLIMITED

        settings = app.settings
        self.search_var = tk.StringVar()
        self.folder_var = tk.StringVar(
            value=str(folder) if self.explicit_paths is None else "")
        self.status_var = tk.StringVar(value="Ready")
        self.zoom_var = tk.StringVar(value="100%")
        self.page_var = tk.StringVar(value="")
        self.probe_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="")
        self.video_position = tk.DoubleVar(value=0.0)
        self.video_time_var = tk.StringVar(value="0:00 / 0:00")
        self.video_meta_var = tk.StringVar(value="VIDEO")
        self.pdf_page_var = tk.StringVar(value="")

        self.kind_filter = tk.StringVar(value=settings.get("filter", D.FILTER_ALL))
        if select is not None:
            # Opened straight onto a file: never let a remembered "Images only"
            # filter hide the very PDF the user double-clicked.
            kind = D.suffix_kind(select.suffix)
            if kind is not None and kind not in D.kinds_for_filter(self.kind_filter.get()):
                self.kind_filter.set(D.FILTER_ALL)
        self.sort_by = tk.StringVar(value=settings.get("sort_by", "name"))
        self.sort_desc = tk.BooleanVar(value=bool(settings.get("sort_desc", False)))
        # How many levels of sub-folders to include: 0 = this folder only,
        # n = n levels down, -1 = the whole tree. See D.scan_folder.
        self.scan_depth = tk.IntVar(value=int(settings.get("scan_depth", 0)))
        self.auto_reload = tk.BooleanVar(value=bool(settings.get("auto_reload", True)))
        self.show_sidebar = tk.BooleanVar(value=bool(settings.get("show_sidebar", True)))
        self.sidebar_auto_hide = tk.BooleanVar(
            value=bool(settings.get("sidebar_auto_hide", False)))
        self.show_info = tk.BooleanVar(value=bool(settings.get("show_info", False)))
        self.show_minimap = tk.BooleanVar(value=bool(settings.get("show_minimap", True)))
        self.smooth = tk.BooleanVar(value=bool(settings.get("smooth", True)))
        self.invert = tk.BooleanVar(value=False)
        self.grayscale = tk.BooleanVar(value=False)
        self.transparency = tk.StringVar(value=settings.get("transparency", "white"))
        self.thumb_size = tk.IntVar(value=int(settings.get("thumb_size", 24)))
        self.density = tk.StringVar(value=settings.get("density", "normal"))
        self.pdf_dpi = tk.DoubleVar(value=float(settings.get("pdf_dpi", 150.0)))
        self.export_dpi = tk.DoubleVar(value=float(settings.get("export_dpi", 300.0)))
        self.slideshow_secs = tk.DoubleVar(value=float(settings.get("slideshow_secs", 4.0)))
        self.slideshow_on = tk.BooleanVar(value=False)
        self.snip_mode = tk.BooleanVar(value=False)
        self.open_fit = tk.StringVar(value=str(settings.get("open_fit", "optimal")))
        self._sidebar_auto_collapsed = False
        self._auto_sidebar_key: Optional[tuple] = None
        self._minimap_job = None
        self.video_loop = tk.BooleanVar(value=bool(settings.get("video_loop", False)))
        self.video_autoplay = tk.BooleanVar(value=bool(settings.get("video_autoplay", True)))
        self.video_frame_step = tk.IntVar(value=max(
            VIDEO_FRAME_STEP_MIN,
            min(VIDEO_FRAME_STEP_MAX, int(settings.get("video_frame_step", 10))),
        ))

        self._update_window_title()
        self._apply_icon()
        self.geometry(app.next_geometry())
        self.minsize(880, 560)
        self.configure(background=self.palette.bg)

        # Text selection and image OCR; consulted after edit tools and snip
        # in every gesture, before panning. The toolbar's OCR button targets
        # it, so it exists before the UI is built (it needs no widgets).
        self.text = TextController(self)
        # Clickable links (PDF link annotations, printed URLs, addresses in
        # an OCR layer); consulted on hover and on a click without drag.
        self.links = LinkController(self)
        # Document outline (bookmarks) docked on the right when available;
        # its panel is built inside _build_ui's right dock.
        self.index = IndexController(self)
        self._build_ui()
        # The edit controller wires itself into the toolbar, the stage rows
        # and (below) the menus, so it must exist between the two builders.
        self.edit = EditController(self)
        self.edit.build_toolbar_button(self.actionbar)
        self._build_menus()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.after(30, self._ensure_on_screen)
        self.after(60, self.rescan)
        self._watch_job = self.after(WATCH_MS, self._watch)

    def _apply_icon(self) -> None:
        icon = self.app.app_dir / "figure-viewer.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except Exception:
                pass

    def _ensure_on_screen(self) -> None:
        """Pull the window back onto a visible monitor if it opened off-screen."""
        try:
            self.update_idletasks()
            x, y = self.winfo_x(), self.winfo_y()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            # Allow negative x for genuine secondary monitors, but not absurd values.
            if x < -sw or y < -sh or x > sw * 2 or y > sh + 200:
                self.geometry("+80+60")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    # -------------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        p = self.palette
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # --- themed menu bar ------------------------------------------------
        self.menubar = MenuBar(self, p)
        self.menubar.grid(row=0, column=0, sticky="ew")
        brand = self.icons.brand(
            self.app.app_dir / "assets" / "figure-viewer-mark-256.png", 21
        )
        if brand is not None:
            ttk.Label(self.menubar, image=brand, style="Panel.TLabel").pack(
                side="left", padx=(9, 3), pady=2
            )
        ttk.Frame(self, style="Rule.TFrame", height=1).grid(row=1, column=0, sticky="ew")

        # --- toolbar --------------------------------------------------------
        bar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 7))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)

        nav = ttk.Frame(bar, style="ToolbarGroup.TFrame", padding=2)
        nav.grid(row=0, column=0, padx=(0, 8))
        toolbar_button(nav, "", self.previous_file, p, "Previous file  (Left)",
                       style="Icon.TButton", image=self.icons.get("previous")).pack(side="left")
        toolbar_button(nav, "", self.next_file, p, "Next file  (Right)",
                       style="Icon.TButton", image=self.icons.get("next")).pack(side="left")
        toolbar_button(nav, "", self.reload_all, p, "Rescan folder and reload  (F5)",
                       style="Icon.TButton", image=self.icons.get("reload")).pack(side="left")

        folder_actions = ttk.Frame(bar, style="Toolbar.TFrame")
        folder_actions.grid(row=0, column=1, padx=(0, 8))
        toolbar_button(folder_actions, "Folder", self.choose_folder, p,
                       "Choose another folder  (Ctrl+O)", image=self.icons.get("folder"),
                       compound="left").pack(side="left")
        toolbar_button(folder_actions, "", self.go_parent, p,
                       "Parent folder  (Backspace)", style="Icon.TButton",
                       image=self.icons.get("up")).pack(side="left", padx=(3, 0))

        entry = ttk.Entry(bar, textvariable=self.folder_var)
        entry.grid(row=0, column=2, sticky="ew")
        entry.bind("<Return>", lambda _e: self.open_typed_folder())
        self.folder_entry = entry

        # A deliberately quiet affordance. It clears the active directory and
        # leaves folder mode, rather than merely deleting text that would be
        # restored by the next refresh.
        self.clear_folder_button = ttk.Button(
            bar, text="×", width=2, style="Icon.TButton", takefocus=False,
            command=self.clear_folder_path)
        self.clear_folder_button.grid(row=0, column=3, padx=(2, 8))
        bind_tooltip(self.clear_folder_button, "Clear folder path", p)

        # With explicitly opened files there is no folder to type, so the
        # empty path box gives way to a chip naming the file's folder;
        # clicking it opens that folder around the file.
        self.path_chip_var = tk.StringVar(value="")
        chip = ttk.Label(bar, textvariable=self.path_chip_var, style="Chip.TLabel",
                         cursor="hand2", image=self.icons.get("folder", tone="muted"),
                         compound="left")
        chip.grid(row=0, column=2, sticky="w")
        chip.grid_remove()
        chip.bind("<Button-1>", lambda _e: self._open_chip_folder())
        chip.bind("<Enter>", lambda _e: chip.state(["active"]))
        chip.bind("<Leave>", lambda _e: chip.state(["!active"]))
        self.path_chip = chip
        self.path_chip_tip = bind_tooltip(chip, "Open the containing folder", p)
        self._sync_path_controls()

        actionbar = ttk.Frame(bar, style="Toolbar.TFrame")
        actionbar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        self.actionbar = actionbar

        zoom = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        zoom.pack(side="left", padx=(0, 8))
        toolbar_button(zoom, "", lambda: self.zoom_by(1 / ZOOM_STEP), p,
                       "Zoom out  (Ctrl+-)", style="Icon.TButton",
                       image=self.icons.get("zoom_out")).pack(side="left")
        toolbar_button(zoom, "", lambda: self.zoom_by(ZOOM_STEP), p,
                       "Zoom in  (Ctrl++)", style="Icon.TButton",
                       image=self.icons.get("zoom_in")).pack(side="left")
        toolbar_button(zoom, "Auto", self.optimal_zoom, p,
                       "Optimal zoom: fill the width  (Ctrl+Shift+9)",
                       style="Icon.TButton",
                       image=self.icons.get("auto_fit")).pack(side="left")
        toolbar_button(zoom, "Fit", self.fit_page, p, "Fit the whole page  (Ctrl+0)",
                       style="Icon.TButton", image=self.icons.get("fit")).pack(side="left")
        toolbar_button(zoom, "1:1", self.actual_size, p,
                       "Actual size, 100 %  (Ctrl+Shift+0)", style="Icon.TButton",
                       image=self.icons.get("actual")).pack(side="left")

        rot = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        rot.pack(side="left", padx=(0, 8))
        toolbar_button(rot, "", lambda: self.rotate(-90), p, "Rotate left  (Ctrl+[)",
                       style="Icon.TButton", image=self.icons.get("rotate_left")).pack(side="left")
        toolbar_button(rot, "", lambda: self.rotate(90), p, "Rotate right  (Ctrl+])",
                       style="Icon.TButton", image=self.icons.get("rotate_right")).pack(side="left")

        # Capture group: Snip (modal, unmistakable while on) and OCR.
        capture = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        capture.pack(side="left", padx=(0, 8))
        self.snip_button = ttk.Checkbutton(
            capture, text="Snip", image=self.icons.get("snip"), compound="left",
            variable=self.snip_mode, style="Group.Modal.TCheckbutton", takefocus=False,
            command=self._snip_mode_changed)
        self.snip_button.pack(side="left")
        bind_tooltip(self.snip_button, "Drag a rectangle to copy that region  (S)", p)
        ocr = toolbar_button(capture, "", self.text.ocr_and_copy, p,
                             "OCR: read the text in this image or scanned page and copy it  "
                             "(Ctrl+Shift+T)", style="Group.Tool.TButton",
                             image=self.icons.get("ocr"), compound="image")
        ocr.pack(side="left")
        self.ocr_button = ocr

        # Edit group: the edit controller and the review tools fill it.
        self.edit_group = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        self.edit_group.pack(side="left", padx=(0, 8))

        # Panel toggles sit together at the far right, mirroring where the
        # panels themselves live; preferences go just before them.
        panels = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        panels.pack(side="right")
        # Icon-only: these toggle panels and preferences, they do not change
        # what the mouse does, so a tooltip is enough and a label is noise.
        files = ttk.Checkbutton(
            panels, image=self.icons.get("files"), compound="image",
            variable=self.show_sidebar, style="Group.Toggle.TCheckbutton", takefocus=False,
            command=self._sidebar_visibility_changed)
        files.pack(side="left")
        self.files_button = files
        bind_tooltip(files, "Files: show or hide the file list  (Ctrl+B)", p)
        index_button = ttk.Checkbutton(
            panels, image=self.icons.get("index"), compound="image",
            variable=self.index.show_index, style="Group.Toggle.TCheckbutton",
            takefocus=False, command=self._apply_layout)
        index_button.pack(side="left")
        self.index_button = index_button
        bind_tooltip(index_button, "Index: the document's bookmarks, when it has any  "
                                   "(Ctrl+Shift+I)", p)
        details = ttk.Checkbutton(
            panels, image=self.icons.get("info"), compound="image",
            variable=self.show_info, style="Group.Toggle.TCheckbutton",
            takefocus=False, command=self._apply_layout)
        details.pack(side="left")
        self.details_button = details
        bind_tooltip(details, "Details: file and page information  (Ctrl+I)", p)

        prefs = ttk.Frame(actionbar, style="ToolbarGroup.TFrame", padding=2)
        prefs.pack(side="right", padx=(0, 8))
        auto = ttk.Checkbutton(prefs, image=self.icons.get("auto"), compound="image",
                               variable=self.auto_reload,
                               style="Group.Toggle.TCheckbutton", takefocus=False)
        auto.pack(side="left")
        bind_tooltip(auto, "Auto reload: refresh when the file changes on disk", p)
        auto_hide = ttk.Checkbutton(
            prefs, image=self.icons.get("autohide"), compound="image",
            variable=self.sidebar_auto_hide, style="Group.Toggle.TCheckbutton",
            takefocus=False, command=self._sidebar_auto_hide_changed)
        auto_hide.pack(side="left")
        bind_tooltip(auto_hide, "Auto-hide: tuck the file list away five seconds after "
                                "you stop using it", p)

        # --- body: sidebar | canvas | info ---------------------------------
        body = ttk.Frame(self, style="TFrame")
        body.grid(row=3, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)      # the canvas takes the slack
        self.body = body

        self.sidebar = self._build_sidebar(body)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self._set_sidebar_width(self._sidebar_px)
        self.sash = self._build_sash(body)

        stage = ttk.Frame(body, style="TFrame")
        stage.grid(row=0, column=2, sticky="nsew")
        stage.rowconfigure(0, weight=1)
        stage.columnconfigure(0, weight=1)
        self.stage = stage
        self.canvas = tk.Canvas(stage, background=p.canvas, highlightthickness=0,
                                borderwidth=0, takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.pdf_controls = self._build_pdf_controls(stage)
        self.video_controls = self._build_video_controls(stage)

        # Right dock: the index (outline) above the details panel, each
        # shown independently; the dock itself appears only when one is.
        # Its width is dragged with a sash, like the file list's.
        self.right_sash = self._build_sash(body, side="right")
        dock = ttk.Frame(body, style="Panel.TFrame")
        dock.columnconfigure(0, weight=1)
        self.right_dock = dock
        self._dock_px = max(DOCK_MIN, min(DOCK_MAX, int(
            self.app.settings.get("dock_width", DOCK_DEFAULT))))
        self._set_dock_width(self._dock_px)
        self.index_panel = self.index.build(dock)
        self.info_panel = self._build_info_panel(dock)

        # --- status bar -----------------------------------------------------
        ttk.Frame(self, style="Rule.TFrame", height=1).grid(row=4, column=0, sticky="ew")
        status = ttk.Frame(self, style="Status.TFrame", padding=(12, 5))
        status.grid(row=5, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").grid(row=0, column=0, sticky="ew")
        self.swatch = tk.Canvas(status, width=13, height=13, highlightthickness=1,
                                highlightbackground=p.border, background=p.panel)
        self.swatch.grid(row=0, column=1, padx=(10, 6))
        # Hidden until the pointer is actually over the image: an empty
        # bordered square sitting in the status bar all day reads as a defect.
        self.swatch.grid_remove()
        ttk.Label(status, textvariable=self.probe_var, style="Mono.TLabel").grid(row=0, column=2, padx=(0, 14))
        ttk.Label(status, textvariable=self.page_var, style="Status.TLabel").grid(row=0, column=3, padx=(0, 14))
        ttk.Label(status, textvariable=self.zoom_var, style="Mono.TLabel").grid(row=0, column=4)

        self._bind_canvas()
        self._apply_layout()

    def _build_pdf_controls(self, master: tk.Misc) -> ttk.Frame:
        """Visible page navigation shown only for multi-page PDF documents."""
        frame = ttk.Frame(master, style="Transport.TFrame", padding=(10, 7))
        centre = ttk.Frame(frame, style="Transport.TFrame")
        centre.pack(anchor="center")
        self.pdf_previous_button = toolbar_button(
            centre, "Previous page", lambda: self.change_pdf_page(-1), self.palette,
            "Previous PDF page  (Page Up)", style="Transport.TButton",
            image=self.icons.get("previous"), compound="left")
        self.pdf_previous_button.pack(side="left")
        ttk.Label(centre, textvariable=self.pdf_page_var, style="Transport.TLabel",
                  width=24, anchor="center").pack(side="left", padx=12)
        self.pdf_next_button = toolbar_button(
            centre, "Next page", lambda: self.change_pdf_page(1), self.palette,
            "Next PDF page  (Page Down)", style="Transport.TButton",
            image=self.icons.get("next"), compound="right")
        self.pdf_next_button.pack(side="left")
        frame.grid(row=1, column=0, sticky="ew")
        frame.grid_remove()
        return frame

    def _build_video_controls(self, master: tk.Misc) -> ttk.Frame:
        """Create the transport bar once and reveal it only for videos."""
        p = self.palette
        frame = ttk.Frame(master, style="Transport.TFrame", padding=(10, 7))
        frame.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(5, weight=1)

        toolbar_button(frame, "", lambda: self.step_video_frames(-1), p,
                       "Previous frame  (Left)", style="Transport.TButton",
                       image=self.icons.get("previous")).grid(row=0, column=0)
        self.video_play_button = toolbar_button(
            frame, "", self.toggle_video, p, "Play or pause  (Space)",
            style="Transport.TButton", image=self.icons.get("play")
        )
        self.video_play_button.grid(row=0, column=1, padx=2)
        toolbar_button(frame, "", lambda: self.step_video_frames(1), p,
                       "Next frame  (Right)", style="Transport.TButton",
                       image=self.icons.get("next")).grid(row=0, column=2)
        toolbar_button(frame, "", self.stop_video_playback, p,
                       "Stop and return to the first frame", style="Transport.TButton",
                       image=self.icons.get("stop")).grid(row=0, column=3, padx=(2, 8))

        self.video_time_label = ttk.Label(
            frame, textvariable=self.video_time_var, style="Transport.TLabel",
            width=14, anchor="e",
        )
        self.video_time_label.grid(row=0, column=4, padx=(0, 8))
        scale = ttk.Scale(frame, from_=0.0, to=1.0, orient="horizontal",
                          variable=self.video_position, takefocus=False,
                          style="Media.Horizontal.TScale")
        scale.grid(row=0, column=5, sticky="ew", padx=(0, 10))
        scale.bind("<ButtonPress-1>", self._video_seek_press)
        scale.bind("<ButtonRelease-1>", self._video_seek_release)
        self.video_scale = scale

        loop = ttk.Checkbutton(
            frame, text="Loop", image=self.icons.get("loop"), compound="left",
            variable=self.video_loop, style="Transport.TCheckbutton", takefocus=False,
            command=self._video_loop_changed,
        )
        loop.grid(row=0, column=6, padx=(0, 10))
        self.video_loop_button = loop
        bind_tooltip(loop, "Restart automatically when the video ends", p)

        meta = ttk.Label(frame, textvariable=self.video_meta_var, style="Media.TLabel")
        meta.grid(row=0, column=7)
        self.video_meta_label = meta
        bind_tooltip(meta, "OpenCV uses FFmpeg for fast in-app video. Audio is not available.", p)
        frame.grid_remove()
        return frame

    def _adapt_video_controls(self, width: int) -> None:
        """Keep the timeline usable when both side panels narrow the stage."""
        if not hasattr(self, "video_meta_label"):
            return
        if width >= 640:
            self.video_meta_label.grid()
        else:
            self.video_meta_label.grid_remove()
        if width >= 520:
            self.video_loop_button.grid()
        else:
            self.video_loop_button.grid_remove()
        if width >= 390:
            self.video_time_label.grid()
        else:
            self.video_time_label.grid_remove()

    def _build_sidebar(self, master: tk.Misc) -> ttk.Frame:
        """File list with search, thumbnails and a count footer."""
        p = self.palette
        frame = ttk.Frame(master, style="Panel.TFrame", padding=(10, 10, 8, 8))
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        header = ttk.Frame(frame, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="FILES", image=self.icons.get("files", tone="muted"),
                  compound="left", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.count_var, style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        # The kind filter lives with the list it filters.
        segment = ttk.Frame(header, style="Card.TFrame", padding=1)
        segment.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        for label, value, tip in (("Images", D.FILTER_IMAGES, "Show images only  (Ctrl+1)"),
                                  ("PDF", D.FILTER_DOCS, "Show PDF and vector documents  (Ctrl+2)"),
                                  ("Video", D.FILTER_VIDEOS, "Show videos only  (Ctrl+3)"),
                                  ("All", D.FILTER_ALL, "Show everything  (Ctrl+4)")):
            radio = ttk.Radiobutton(segment, text=label, value=value, variable=self.kind_filter,
                                    style="Segment.TRadiobutton", takefocus=False,
                                    command=self.rescan)
            radio.pack(side="left")
            bind_tooltip(radio, tip, p)
        ttk.Frame(header, style="Rule.TFrame", height=1).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        search = ttk.Entry(frame, textvariable=self.search_var)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.search_entry = search
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        bind_tooltip(search, "Filter by name. Space-separated words are ANDed; "
                             "* and ? wildcards work too.  (Ctrl+F)", p)
        self._install_placeholder(search, self.search_var, "Search files…")

        holder = ttk.Frame(frame, style="Panel.TFrame")
        holder.grid(row=2, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(holder, columns=("meta",), show="tree", selectmode="browse",
                                 takefocus=True, style=LIST_FLAT)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.column("#0", width=SIDEBAR_DEFAULT, stretch=True, anchor="w")
        self.tree.column("meta", width=0, stretch=False)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview,
                               style="Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda _e: self.open_default_app())
        self.tree.bind("<Button-3>", self._file_context_menu)
        self.tree.bind("<<TreeviewOpen>>", self._on_group_toggle)
        self.tree.bind("<<TreeviewClose>>", self._on_group_toggle)
        self.tree.tag_configure("doc", foreground=self.palette.accent)
        self.tree.tag_configure("video", foreground=self.palette.media)
        # Folder rows are headers, not files: semibold and muted, so three
        # levels of nesting still read as structure rather than more entries.
        self.tree.tag_configure("group", foreground=self.palette.muted, font="FVuiBold")

        # No footer buttons: Copy, Path and Open live in the Edit menu, the
        # context menus and the shortcuts, and a row of them under the list
        # only competed with the list for attention.
        for widget in (frame, search, holder, self.tree):
            widget.bind("<Motion>", self._sidebar_activity, add="+")
            widget.bind("<Button>", self._sidebar_activity, add="+")
            widget.bind("<Key>", self._sidebar_activity, add="+")
        return frame

    def _build_sash(self, master: tk.Misc, side: str = "left") -> ttk.Frame:
        """A draggable divider: file list | canvas (left) or canvas | dock
        (right). Both share one drag handler; the side decides the sign."""
        sash = ttk.Frame(master, style="Sash.TFrame", width=6, cursor="sb_h_double_arrow")
        sash.grid(row=0, column=1 if side == "left" else 3, sticky="ns")
        sash.bind("<Enter>", lambda _e: sash.configure(style="SashActive.TFrame"))
        sash.bind("<Leave>", lambda _e: sash.configure(style="Sash.TFrame"))
        sash.bind("<ButtonPress-1>", lambda e: self._sash_press(e, side))
        sash.bind("<B1-Motion>", self._sash_drag)
        sash.bind("<ButtonRelease-1>", self._sash_release)
        if side == "left":
            sash.bind("<Double-1>", lambda _e: self._set_sidebar_width(SIDEBAR_DEFAULT))
            bind_tooltip(sash, "Drag to resize the file list  ·  double-click to reset",
                         self.palette)
        else:
            sash.bind("<Double-1>", lambda _e: self._set_dock_width(DOCK_DEFAULT))
            bind_tooltip(sash, "Drag to resize the index and details panels  ·  "
                               "double-click to reset", self.palette)
        return sash

    def _set_dock_width(self, width: int) -> None:
        """Fix the right dock to ``width`` pixels (same technique as the
        sidebar: the frame owns the width, propagation off)."""
        width = max(DOCK_MIN, min(DOCK_MAX, int(width)))
        self._dock_px = width
        try:
            self.right_dock.configure(width=width)
            self.right_dock.grid_propagate(False)
        except tk.TclError:
            return
        self.app.settings["dock_width"] = width

    def _set_sidebar_width(self, width: int) -> None:
        """Fix the sidebar to ``width`` pixels.

        The width lives on the *frame*, with geometry propagation switched off,
        rather than on the tree column: a column with ``stretch=True`` is
        re-fitted to its widget on the next configure, so a width written there
        is undone before the user lets go of the mouse.
        """
        width = max(SIDEBAR_MIN, min(SIDEBAR_MAX, int(width)))
        self._sidebar_px = width
        try:
            self.sidebar.configure(width=width)
            self.sidebar.grid_propagate(False)
        except tk.TclError:
            return
        self.app.settings["sidebar_width"] = width
        try:
            self._update_count()
        except AttributeError:
            pass          # called once from _build_ui, before the list exists

    def _sash_press(self, event, side: str = "left") -> None:
        width = self._sidebar_px if side == "left" else self._dock_px
        self._sash_origin = (event.x_root, width, side)

    def _sash_drag(self, event) -> None:
        if not self._sash_origin:
            return
        start_x, start_width, side = self._sash_origin
        delta = event.x_root - start_x
        if side == "left":
            self._set_sidebar_width(start_width + delta)
        else:
            # The dock grows when its sash moves left.
            self._set_dock_width(start_width - delta)

    def _sash_release(self, _event=None) -> None:
        self._sash_origin = None

    def _build_info_panel(self, master: tk.Misc) -> ttk.Frame:
        """Right-hand metadata panel (hidden by default)."""
        p = self.palette
        frame = ttk.Frame(master, style="Panel.TFrame", padding=(12, 10, 8, 8))
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        header = ttk.Frame(frame, style="Panel.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="DETAILS", image=self.icons.get("info", tone="muted"),
                  compound="left", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Frame(header, style="Rule.TFrame", height=1).grid(
            row=1, column=0, sticky="ew", pady=(7, 0))
        # A small requested height: the dock's grid weights, not this
        # widget's natural size, decide how it shares the column with the
        # index panel above it.
        text = tk.Text(frame, width=32, height=8, wrap="word", background=p.panel,
                       foreground=p.text, insertbackground=p.text, relief="flat",
                       highlightthickness=0, font="FVsmall", padx=0, pady=8,
                       spacing1=1, spacing3=3, cursor="arrow")
        text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        # PDF metadata easily overflows the panel; without this the tail was
        # simply invisible, with no hint that anything had been cut off.
        info_scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview,
                                    style="Vertical.TScrollbar")
        info_scroll.grid(row=1, column=1, sticky="ns", pady=(8, 0))
        text.configure(yscrollcommand=info_scroll.set)
        text.tag_configure("key", foreground=p.muted)
        text.tag_configure("value", foreground=p.text)
        text.configure(state="disabled")
        self.info_text = text
        return frame

    # ----------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        p = self.palette

        file_menu = self.menubar.add_menu("File", underline=0)
        file_menu.add_command(label="New Window", accelerator="Ctrl+N", command=self.app.new_window)
        file_menu.add_command(label="Duplicate Window", accelerator="Ctrl+Shift+N",
                              command=self.duplicate_window)
        file_menu.add_separator()
        file_menu.add_command(label="Open Folder…", accelerator="Ctrl+O", command=self.choose_folder)
        file_menu.add_command(label="Open File…", accelerator="Ctrl+Shift+O", command=self.choose_file)
        self.recent_files_menu = tk.Menu(file_menu)
        self.recent_folders_menu = tk.Menu(file_menu)
        style_menu(self.recent_files_menu, p)
        style_menu(self.recent_folders_menu, p)
        file_menu.add_cascade(label="Recent Files", menu=self.recent_files_menu)
        file_menu.add_cascade(label="Recent Folders", menu=self.recent_folders_menu)
        file_menu.add_command(label="Parent Folder", accelerator="Backspace", command=self.go_parent)
        file_menu.add_separator()
        file_menu.add_command(label="Save PDF", accelerator="Ctrl+S",
                              command=lambda: self.edit.save())
        file_menu.add_command(label="Save PDF As…", command=self.edit.save_as)
        file_menu.add_command(label="Export To Word / Excel / PowerPoint…",
                              accelerator="Ctrl+Shift+E",
                              command=self.edit.open_export)
        file_menu.add_command(label="Certify with Certificate…",
                              command=self.edit.sign_with_certificate)
        file_menu.add_command(label="Merge PDFs at End…",
                              command=self.edit.merge_append_files)
        file_menu.add_separator()
        file_menu.add_command(label="Save Page / Frame As…",
                              command=self.save_page_as)
        file_menu.add_command(label="Save Snip As…", accelerator="Ctrl+Shift+S", command=self.save_snip_as)
        file_menu.add_command(label="Export All Pages…", command=self.export_all_pages)
        file_menu.add_command(label="Save Image as Searchable PDF…",
                              command=self.text.save_searchable_pdf)
        file_menu.add_separator()
        file_menu.add_command(label="Open in Default App", accelerator="Enter", command=self.open_default_app)
        file_menu.add_command(label="Reveal in Explorer", accelerator="Ctrl+E", command=self.reveal_in_explorer)
        file_menu.add_separator()
        file_menu.add_command(label="Close Window", accelerator="Ctrl+W", command=self.close)
        file_menu.add_command(label="Exit", accelerator="Alt+F4", command=self.app.quit_all)

        edit_menu = self.menubar.add_menu("Edit", underline=0)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z",
                              command=self.edit.undo, state="disabled")
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y",
                              command=self.edit.redo, state="disabled")
        # The controller rewrites these two labels live with the operation
        # name ("Undo Add highlight"), so it needs their menu and indices.
        self._edit_menu_indices = (edit_menu, 0, 1)
        edit_menu.add_separator()
        edit_menu.add_command(label="Edit PDF", accelerator="E",
                              command=self.edit.toggle_mode)
        edit_menu.add_command(label="Find and Replace…", accelerator="Ctrl+H",
                              command=self.edit.open_find)
        edit_menu.add_command(label="Discard PDF Edits",
                              command=self.edit.discard_session)
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy Selected Text", accelerator="Ctrl+C",
                              command=self.text.copy_selection)
        edit_menu.add_command(label="Select All Text", accelerator="Ctrl+A",
                              command=self.text.select_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy Image / Video Frame", accelerator="Ctrl+C",
                              command=self.copy_image)
        edit_menu.add_command(label="Copy Visible View", accelerator="Ctrl+Shift+V", command=self.copy_view)
        edit_menu.add_command(label="Copy Last Snip", accelerator="Ctrl+Shift+C", command=self.copy_last_snip)
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy File Path", accelerator="Ctrl+Shift+P", command=self.copy_path)
        edit_menu.add_command(label="Copy File (paste into Explorer)", command=self.copy_file_object)
        edit_menu.add_command(label="Copy Folder Path", command=self.copy_folder_path)
        edit_menu.add_command(label="Copy Page Text", command=self.copy_page_text)
        edit_menu.add_command(label="Copy Image Text (OCR)", accelerator="Ctrl+Shift+T",
                              command=self.text.ocr_and_copy)
        edit_menu.add_separator()
        edit_menu.add_command(label="Snip Region", accelerator="S", command=lambda: self.snip_mode.set(True) or self._snip_mode_changed())
        edit_menu.add_command(label="Find in File List", accelerator="Ctrl+F", command=self.focus_search)

        view_menu = self.menubar.add_menu("View", underline=0)
        view_menu.add_command(label="Optimal Zoom", accelerator="Ctrl+Shift+9",
                              command=self.optimal_zoom)
        view_menu.add_command(label="Fit Page", accelerator="Ctrl+0", command=self.fit_page)
        view_menu.add_command(label="Fit Width", accelerator="Ctrl+9", command=self.fit_width)
        view_menu.add_command(label="Actual Size", accelerator="Ctrl+Shift+0", command=self.actual_size)
        view_menu.add_command(label="Zoom In", accelerator="Ctrl++", command=lambda: self.zoom_by(ZOOM_STEP))
        view_menu.add_command(label="Zoom Out", accelerator="Ctrl+-", command=lambda: self.zoom_by(1 / ZOOM_STEP))
        open_fit_menu = tk.Menu(view_menu)
        style_menu(open_fit_menu, p)
        for value, label, _pdf_mode, _image_mode in OPEN_FIT_CHOICES:
            open_fit_menu.add_radiobutton(label=label, value=value,
                                          variable=self.open_fit,
                                          command=self._open_fit_changed)
        view_menu.add_cascade(label="Zoom on Open", menu=open_fit_menu)
        view_menu.add_separator()
        view_menu.add_command(label="Rotate Left", accelerator="Ctrl+[", command=lambda: self.rotate(-90))
        view_menu.add_command(label="Rotate Right", accelerator="Ctrl+]", command=lambda: self.rotate(90))
        view_menu.add_checkbutton(label="Flip Horizontal", variable=self.flip_h_var(), command=self.redraw_now)
        view_menu.add_checkbutton(label="Flip Vertical", variable=self.flip_v_var(), command=self.redraw_now)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Invert Colours", accelerator="I", variable=self.invert,
                                  command=self.redraw_now)
        view_menu.add_checkbutton(label="Grayscale", variable=self.grayscale, command=self.redraw_now)
        view_menu.add_checkbutton(label="Smooth Scaling", variable=self.smooth, command=self.redraw_now)
        transparency = tk.Menu(view_menu)
        style_menu(transparency, p)
        for label, value in (("White", "white"), ("Checkerboard", "checker"), ("Dark", "dark")):
            transparency.add_radiobutton(label=label, value=value, variable=self.transparency,
                                         command=self.redraw_now)
        view_menu.add_cascade(label="Transparent Background", menu=transparency)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Sidebar", accelerator="Ctrl+B", variable=self.show_sidebar,
                                  command=self._sidebar_visibility_changed)
        view_menu.add_checkbutton(label="Auto-hide File List (5 seconds)",
                                  variable=self.sidebar_auto_hide,
                                  command=self._sidebar_auto_hide_changed)
        view_menu.add_checkbutton(label="Details Panel", accelerator="Ctrl+I", variable=self.show_info,
                                  command=self._apply_layout)
        view_menu.add_checkbutton(label="Index Panel (when the PDF has one)",
                                  accelerator="Ctrl+Shift+I",
                                  variable=self.index.show_index,
                                  command=self._apply_layout)
        view_menu.add_checkbutton(label="Overview Map", variable=self.show_minimap, command=self.redraw_now)
        thumbs = tk.Menu(view_menu)
        style_menu(thumbs, p)
        for label, value in (("Off", 0), ("Tiny", 16), ("Small", 24),
                             ("Medium", 36), ("Large", 56)):
            thumbs.add_radiobutton(label=label, value=value, variable=self.thumb_size,
                                   command=self._thumb_size_changed)
        view_menu.add_cascade(label="Thumbnails", menu=thumbs)
        density = tk.Menu(view_menu)
        style_menu(density, p)
        for label, value in (("Compact", "compact"), ("Normal", "normal"),
                             ("Comfortable", "comfortable")):
            density.add_radiobutton(label=label, value=value, variable=self.density,
                                    command=self._thumb_size_changed)
        view_menu.add_cascade(label="List Density", menu=density)
        view_menu.add_separator()
        view_menu.add_command(label="Toggle Theme", accelerator="Ctrl+T", command=self.app.toggle_theme)
        view_menu.add_command(label="Full Screen", accelerator="F11", command=self.toggle_fullscreen)

        go_menu = self.menubar.add_menu("Go", underline=0)
        go_menu.add_command(label="Next File", accelerator="Down", command=self.next_file)
        go_menu.add_command(label="Previous File", accelerator="Up", command=self.previous_file)
        go_menu.add_command(label="First File", accelerator="Home", command=lambda: self.select_index(0))
        go_menu.add_command(label="Last File", accelerator="End", command=lambda: self.select_index(len(self.filtered) - 1))
        go_menu.add_separator()
        go_menu.add_command(label="Next Page", accelerator="PageDown", command=lambda: self.change_page(1))
        go_menu.add_command(label="Previous Page", accelerator="PageUp", command=lambda: self.change_page(-1))
        go_menu.add_command(label="Go to Page…", accelerator="Ctrl+G", command=self.goto_page)

        tools_menu = self.menubar.add_menu("Tools", underline=0)
        sort_menu = tk.Menu(tools_menu)
        style_menu(sort_menu, p)
        for label, value in (("Name", "name"), ("Date Modified", "modified"),
                             ("Size", "size"), ("Type", "kind")):
            sort_menu.add_radiobutton(label=label, value=value, variable=self.sort_by, command=self.rescan)
        sort_menu.add_separator()
        sort_menu.add_checkbutton(label="Descending", variable=self.sort_desc, command=self.rescan)
        tools_menu.add_cascade(label="Sort By", menu=sort_menu)
        depth_menu = tk.Menu(tools_menu)
        style_menu(depth_menu, p)
        for label, value in DEPTH_CHOICES:
            depth_menu.add_radiobutton(label=label, value=value, variable=self.scan_depth,
                                       command=self._depth_changed)
        tools_menu.add_cascade(label="Subfolder Depth", menu=depth_menu)
        tools_menu.add_checkbutton(label="Auto Reload on Change", variable=self.auto_reload)
        tools_menu.add_checkbutton(label="Autoplay Videos", variable=self.video_autoplay,
                                   command=self._video_autoplay_changed)
        tools_menu.add_command(label="Video Frame Jump…",
                               command=self.set_video_frame_step)
        tools_menu.add_separator()
        tools_menu.add_checkbutton(label="Slideshow", accelerator="F9", variable=self.slideshow_on,
                                   command=self._slideshow_changed)
        tools_menu.add_command(label="Slideshow Interval…", command=self.set_slideshow_interval)
        tools_menu.add_separator()
        tools_menu.add_command(label="PDF Render Quality…", command=self.set_pdf_dpi)
        tools_menu.add_command(label="Copy / Export DPI…", command=self.set_export_dpi)
        tools_menu.add_separator()
        tools_menu.add_command(label="Recognise Text (OCR)…",
                               command=self.text.open_ocr)
        tools_menu.add_command(label="Identity Presets…",
                               command=self.edit.open_identities)
        tools_menu.add_separator()
        tools_menu.add_command(label="Rescan Folder", accelerator="F5", command=self.reload_all)
        tools_menu.add_command(label="Clear Caches", command=self.clear_caches)
        tools_menu.add_separator()
        tools_menu.add_command(label="Set as Default PDF Reader…",
                               command=self.register_file_types)

        help_menu = self.menubar.add_menu("Help", underline=0)
        help_menu.add_command(label="Keyboard Shortcuts", accelerator="F1", command=self.show_shortcuts)
        help_menu.add_command(label="About", command=self.show_about)

        self._refresh_recent_menu()

    def flip_h_var(self) -> tk.BooleanVar:
        """Lazily expose ``flip_h`` as a Tk variable for the menu checkbutton."""
        if not hasattr(self, "_flip_h_var"):
            self._flip_h_var = tk.BooleanVar(value=self.flip_h)
            self._flip_h_var.trace_add("write", lambda *_: setattr(self, "flip_h", self._flip_h_var.get()))
        return self._flip_h_var

    def flip_v_var(self) -> tk.BooleanVar:
        if not hasattr(self, "_flip_v_var"):
            self._flip_v_var = tk.BooleanVar(value=self.flip_v)
            self._flip_v_var.trace_add("write", lambda *_: setattr(self, "flip_v", self._flip_v_var.get()))
        return self._flip_v_var

    def _refresh_recent_menu(self) -> None:
        """Rebuild the independent recent-file and recent-folder cascades."""
        menus = (
            (self.recent_files_menu, "recent_files", self.add_file),
            (self.recent_folders_menu, "recent_folders", self.set_folder),
        )
        for menu, key, command in menus:
            menu.delete(0, "end")
            recents = self.app.settings.get(key, [])
            existing = [path for path in recents if Path(path).exists()]
            if not existing:
                menu.add_command(label="(empty)", state="disabled")
                continue
            for path in existing[:12]:
                menu.add_command(label=path,
                                 command=lambda p=path, fn=command: fn(Path(p)))
            menu.add_separator()
            menu.add_command(label="Clear List",
                             command=lambda setting=key: self._clear_recent(setting))

    def _clear_recent(self, key: str) -> None:
        self.app.settings[key] = []
        self.app.broadcast_recent()

    def _refresh_signature_ui(self) -> None:
        """App broadcast: another window changed the signature library."""
        self.edit.refresh_signature_ui()

    def _refresh_identity_ui(self) -> None:
        """App broadcast: another window changed the identity presets."""
        self.edit.refresh_identity_ui()

    # ------------------------------------------------------------- bindings
    def _bind_keys(self) -> None:
        """Window-level shortcuts.

        ``_typing`` guards the handlers that would otherwise steal keystrokes
        from the search box and the folder entry.
        """
        bind = self.bind

        def guard(func):
            def handler(event=None):
                if self._typing():
                    return None
                func()
                return "break"
            return handler

        bind("<Control-n>", lambda e: self.app.new_window())
        bind("<Control-N>", lambda e: self.duplicate_window())
        bind("<Control-o>", lambda e: self.choose_folder())
        bind("<Control-O>", lambda e: self.choose_file())
        bind("<Control-w>", lambda e: self.close())
        bind("<Control-s>", lambda e: self._on_ctrl_s())
        bind("<Control-S>", lambda e: self.save_snip_as())
        bind("<Control-z>", guard(self.edit.undo))
        bind("<Control-y>", guard(self.edit.redo))
        bind("<Control-Z>", guard(self.edit.redo))
        bind("<Control-h>", lambda e: self.edit.open_find())
        bind("<Control-E>", lambda e: self.edit.open_export())
        bind("<Control-e>", lambda e: self.reveal_in_explorer())
        bind("<Control-f>", lambda e: self.focus_search())
        bind("<Control-g>", lambda e: self.goto_page())
        bind("<Control-b>", lambda e: self.toggle_sidebar())
        bind("<Control-i>", lambda e: (self.show_info.set(not self.show_info.get()), self._apply_layout()))
        bind("<Control-t>", lambda e: self.app.toggle_theme())
        bind("<Control-c>", self._copy_shortcut)
        bind("<Control-a>", guard(self.text.select_all))
        bind("<Control-T>", guard(self.text.ocr_and_copy))
        bind("<Control-I>", guard(self.index.toggle))
        bind("<Control-Alt-h>", guard(self.edit.review.action))
        bind("<Control-Alt-m>", guard(lambda: self.edit.review.action("comment")))
        bind("<Control-Alt-r>", guard(self.edit.review.toggle_panel))
        bind("<Control-C>", lambda e: self.copy_last_snip())
        bind("<Control-P>", lambda e: self.copy_path())
        bind("<Control-V>", lambda e: self.copy_view())
        bind("<Control-l>", lambda e: self._focus_folder())
        bind("<Control-R>", lambda e: self.toggle_subfolders())

        bind("<Control-plus>", lambda e: self.zoom_by(ZOOM_STEP))
        bind("<Control-equal>", lambda e: self.zoom_by(ZOOM_STEP))
        bind("<Control-KP_Add>", lambda e: self.zoom_by(ZOOM_STEP))
        bind("<Control-minus>", lambda e: self.zoom_by(1 / ZOOM_STEP))
        bind("<Control-KP_Subtract>", lambda e: self.zoom_by(1 / ZOOM_STEP))
        bind("<Control-0>", lambda e: self.fit_page())
        bind("<Control-9>", lambda e: self.fit_width())
        bind("<Control-parenleft>", lambda e: self.optimal_zoom())
        bind("<Control-parenright>", lambda e: self.actual_size())
        bind("<Control-bracketleft>", lambda e: self.rotate(-90))
        bind("<Control-bracketright>", lambda e: self.rotate(90))
        bind("<Control-Key-1>", lambda e: self._set_filter(D.FILTER_IMAGES))
        bind("<Control-Key-2>", lambda e: self._set_filter(D.FILTER_DOCS))
        bind("<Control-Key-3>", lambda e: self._set_filter(D.FILTER_VIDEOS))
        bind("<Control-Key-4>", lambda e: self._set_filter(D.FILTER_ALL))

        bind("<Left>", guard(self._previous_action))
        bind("<Right>", guard(self._next_action))
        bind("<Up>", guard(self._video_jump_back_or_previous_file))
        bind("<Down>", guard(self._video_jump_forward_or_next_file))
        bind("<Home>", guard(lambda: self.select_index(0)))
        bind("<End>", guard(lambda: self.select_index(len(self.filtered) - 1)))
        bind("<Prior>", guard(lambda: self.change_page(-1)))
        bind("<Next>", guard(lambda: self.change_page(1)))
        bind("<BackSpace>", guard(self.go_parent))
        bind("<Return>", guard(self._on_return_key))
        bind("<F1>", lambda e: self.show_shortcuts())
        bind("<F5>", lambda e: self.reload_all())
        bind("<F9>", lambda e: (self.slideshow_on.set(not self.slideshow_on.get()), self._slideshow_changed()))
        bind("<F11>", lambda e: self.toggle_fullscreen())
        bind("<Escape>", self._on_escape)
        bind("<space>", guard(self.toggle_video))
        bind("<s>", guard(lambda: (self.snip_mode.set(not self.snip_mode.get()), self._snip_mode_changed())))
        bind("<i>", guard(lambda: (self.invert.set(not self.invert.get()), self.redraw_now())))
        bind("<Delete>", guard(self._on_delete_key))
        bind("<e>", guard(self.edit.toggle_mode))
        # Single-key tool switches; inert outside edit mode and inside text
        # fields (set_tool checks edit.active, guard checks _typing).
        for key, tool in (("v", "select"), ("h", "highlight"),
                          ("u", "underline"), ("k", "strikeout"),
                          ("q", "squiggly"), ("t", "text"), ("n", "note"),
                          ("p", "ink"), ("r", "rect"), ("o", "ellipse"),
                          ("l", "line"), ("a", "arrow"), ("x", "redact")):
            bind(f"<{key}>", guard(lambda t=tool: self.edit.set_tool(t)))

    def _on_ctrl_s(self) -> None:
        """Ctrl+S: save PDF edits when a session needs it, else legacy save."""
        if not self.edit.save_shortcut():
            self.save_page_as()

    def _on_delete_key(self) -> None:
        """Delete removes the selected annotation in edit mode, else the file."""
        if not self.edit.on_delete():
            self.delete_current()

    def _on_return_key(self) -> None:
        """Return commits a pending signature placement, else opens the file."""
        if not self.edit.commit_placement():
            self.open_default_app()

    def _bind_canvas(self) -> None:
        canvas = self.canvas
        canvas.bind("<Configure>", self._on_canvas_configure)
        canvas.bind("<MouseWheel>", self._on_wheel)
        canvas.bind("<Shift-MouseWheel>", lambda e: self._pan_by(-e.delta * 0.6, 0))
        canvas.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<ButtonPress-2>", self._on_middle_press)
        canvas.bind("<B2-Motion>", self._on_middle_drag)
        canvas.bind("<ButtonRelease-2>", lambda e: self._end_pan())
        canvas.bind("<Double-1>", self._on_double_click)
        canvas.bind("<Triple-1>", self._on_triple_click)
        canvas.bind("<Button-3>", self._canvas_context_menu)
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self._set_probe(""))
        canvas.bind("<Enter>", lambda e: canvas.focus_set())

    def _typing(self) -> bool:
        """True when a text field has focus, so navigation keys stay literal."""
        widget = self.focus_get()
        return isinstance(widget, (ttk.Entry, tk.Entry, tk.Text, ttk.Combobox))

    # ------------------------------------------------------------- layout
    def _apply_layout(self) -> None:
        """Show or hide the sidebar and details panel."""
        if self.show_sidebar.get() and not self._sidebar_auto_collapsed:
            self.sidebar.grid(row=0, column=0, sticky="nsew")
            self.sash.grid(row=0, column=1, sticky="ns")
            self.body.columnconfigure(0, weight=0, minsize=SIDEBAR_MIN)
        else:
            self.sidebar.grid_remove()
            self.sash.grid_remove()
            self.body.columnconfigure(0, minsize=0)
        show_index = self.index.visible()
        show_info = self.show_info.get()
        dock = self.right_dock
        if show_index:
            self.index_panel.grid(row=0, column=0, sticky="nsew")
            dock.rowconfigure(0, weight=3 if show_info else 1)
        else:
            self.index_panel.grid_remove()
            dock.rowconfigure(0, weight=0)
        if show_info:
            self.info_panel.grid(row=1, column=0, sticky="nsew")
            dock.rowconfigure(1, weight=2 if show_index else 1)
            self._update_info()
        else:
            self.info_panel.grid_remove()
            dock.rowconfigure(1, weight=0)
        if show_index or show_info:
            self.right_sash.grid(row=0, column=3, sticky="ns")
            dock.grid(row=0, column=4, sticky="nsew")
        else:
            self.right_sash.grid_remove()
            dock.grid_remove()
        self.app.settings["show_sidebar"] = self.show_sidebar.get()
        self.app.settings["show_info"] = self.show_info.get()
        self.app.settings["show_index"] = self.index.show_index.get()
        if self.show_sidebar.get():
            self._schedule_sidebar_hide()
        else:
            self._cancel_sidebar_hide()

    def toggle_sidebar(self) -> None:
        """Toggle the file list from the toolbar, menu, or Ctrl+B."""
        self.show_sidebar.set(not self.show_sidebar.get())
        self._sidebar_visibility_changed()

    def _sidebar_visibility_changed(self) -> None:
        # A deliberate toggle always wins over the single-file collapse.
        self._sidebar_auto_collapsed = False
        self._apply_layout()
        if self.show_sidebar.get():
            self.toast("File list shown" +
                       (" for 5 seconds" if self.sidebar_auto_hide.get() else ""))

    def _sidebar_auto_hide_changed(self) -> None:
        """Enable or disable the five-second transient file-list mode."""
        enabled = self.sidebar_auto_hide.get()
        self.app.settings["sidebar_auto_hide"] = enabled
        # Changing the mode always reveals the list, making the result visible
        # and ensuring disabling auto-hide restores a stable reading layout.
        self.show_sidebar.set(True)
        self._apply_layout()
        self.toast("File list will hide after 5 seconds" if enabled
                   else "File list will stay visible")

    def _sidebar_activity(self, _event=None) -> None:
        """Keep the transient list open while the user is interacting with it."""
        self._schedule_sidebar_hide()

    def _schedule_sidebar_hide(self) -> None:
        self._cancel_sidebar_hide()
        if self.sidebar_auto_hide.get() and self.show_sidebar.get() and not self._destroyed:
            self._sidebar_hide_job = self.after(
                SIDEBAR_AUTO_HIDE_MS, self._auto_hide_sidebar)

    def _cancel_sidebar_hide(self) -> None:
        if self._sidebar_hide_job:
            try:
                self.after_cancel(self._sidebar_hide_job)
            except Exception:
                pass
            self._sidebar_hide_job = None

    def _auto_hide_sidebar(self) -> None:
        self._sidebar_hide_job = None
        if not self.sidebar_auto_hide.get() or not self.show_sidebar.get():
            return
        self.show_sidebar.set(False)
        self._apply_layout()

    def toggle_fullscreen(self) -> None:
        """Borderless full screen; Esc or F11 returns."""
        current = bool(self.attributes("-fullscreen"))
        self.attributes("-fullscreen", not current)
        self.toast("Full screen - press F11 or Esc to exit" if not current else "")

    # ------------------------------------------------------- folder handling
    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self, initialdir=str(self.folder),
                                         title="Choose a folder", mustexist=True)
        if chosen:
            self.set_folder(Path(chosen))

    def choose_file(self) -> None:
        patterns = " ".join(
            f"*{s}" for s in sorted(D.RASTER_SUFFIXES | D.VECTOR_SUFFIXES | D.VIDEO_SUFFIXES)
        )
        chosen = filedialog.askopenfilenames(
            parent=self, initialdir=str(self.folder), title="Open files",
            filetypes=[("Supported files", patterns), ("All files", "*.*")])
        if chosen:
            self.add_files([Path(path) for path in chosen])

    def open_typed_folder(self) -> None:
        raw = self.folder_var.get().strip().strip('"')
        if not raw:
            self.clear_folder_path()
            return
        path = Path(raw).expanduser()
        if path.is_file():
            self.add_file(path)
        else:
            self.set_folder(path)

    def duplicate_window(self) -> None:
        """Clone this window without changing folder-versus-document mode."""
        self.app.new_window(
            self.folder,
            self.current.path if self.current else None,
            explicit_paths=(list(self.explicit_paths)
                            if self.explicit_paths is not None else None),
        )

    def add_file(self, path: Path) -> None:
        """Append one explicitly opened file and select it."""
        self.add_files([path])

    def add_files(self, paths: list[Path]) -> None:
        """Append supported files without scanning any parent directory."""
        resolved: list[Path] = []
        for raw in paths:
            try:
                path = raw.expanduser().resolve()
            except OSError:
                path = raw
            if path.is_file() and D.suffix_kind(path.suffix) is not None:
                resolved.append(path)
        if not resolved:
            return

        # Leaving a folder view keeps the document being viewed, not every
        # sibling that happened to be present in the directory listing.
        if self.explicit_paths is None:
            self.explicit_paths = [self.current.path] if self.current else []
            self.folder_var.set("")
            self.clear_folder_button.state(["disabled"])
        known = {os.path.normcase(str(path)) for path in self.explicit_paths}
        for path in resolved:
            identity = os.path.normcase(str(path))
            if identity not in known:
                self.explicit_paths.append(path)
                known.add(identity)
        self.app.push_recent_files(resolved)
        target = resolved[-1]
        self.folder = target.parent
        kind = D.suffix_kind(target.suffix)
        if kind is not None and kind not in D.kinds_for_filter(self.kind_filter.get()):
            self.kind_filter.set(D.FILTER_ALL)
        self.pending_select = target
        self.rescan(keep_selection=False)

    def clear_folder_path(self) -> None:
        """Leave folder mode and reset the active listing to an empty session."""
        self._list_seq += 1
        self.explicit_paths = []
        self.folder_var.set("")
        self._sync_path_controls()
        self.clear_folder_button.state(["disabled"])
        self._stop_video_session()
        self.current = None
        self.document = None
        self.page = 0
        self.pending_select = None
        self.entries = []
        self.canvas.delete("all")
        self.pdf_controls.grid_remove()
        self._update_window_title()
        self._apply_filter()
        self.folder_entry.focus_set()

    def reveal_path(self, target: Path) -> None:
        """Open an explicit file or switch to an explicitly requested folder."""
        try:
            target = target.expanduser().resolve()
        except Exception:
            pass

        if target.is_dir():
            if self.explicit_paths is not None or target != self.folder:
                self.set_folder(target)
            else:
                # Same folder handed over again: still refresh, so a launch
                # from Explorer never looks like nothing happened.
                self.rescan()
            return

        self.add_file(target)

    def go_parent(self) -> None:
        base = (self.current.path.parent
                if self.explicit_paths is not None and self.current else self.folder)
        parent = base.parent
        if parent != base:
            self.set_folder(parent)

    def set_folder(self, folder: Path, select: Optional[Path] = None) -> None:
        """Switch to another folder, remembering it in the recent list."""
        try:
            folder = folder.expanduser().resolve()
        except Exception:
            pass
        if not folder.is_dir():
            messagebox.showerror("Folder not found", str(folder), parent=self)
            self.folder_var.set(str(self.folder) if self.explicit_paths is None else "")
            return
        self.folder = folder
        self.explicit_paths = None
        self.folder_var.set(str(folder))
        self.clear_folder_button.state(["!disabled"])
        self._sync_path_controls()
        self._update_window_title()
        self._stop_video_session()
        self.current = None
        self.document = None
        self.page = 0
        self.pending_select = select
        self.canvas.delete("all")
        self.pdf_controls.grid_remove()
        self.app.push_recent_folder(folder)
        self.rescan()

    # ------------------------------------------------------------- scanning
    def rescan(self, *, keep_selection: bool = True) -> None:
        """Rescan the folder on a worker thread and refresh the file list."""
        if self._destroyed:
            return
        self._list_seq += 1
        sequence = self._list_seq
        self._rescan_pending += 1
        folder = self.folder
        explicit_paths = (None if self.explicit_paths is None else
                          tuple(self.explicit_paths))
        kinds = D.kinds_for_filter(self.kind_filter.get())
        depth = self.scan_depth.get()
        sort_by = self.sort_by.get()
        descending = self.sort_desc.get()
        keep = self.current.path if (keep_selection and self.current) else self.pending_select

        def job():
            if self._destroyed or sequence != self._list_seq:
                return [], False
            if explicit_paths is not None:
                return D.entries_for_paths(list(explicit_paths), kinds=kinds), False
            return D.scan_folder(folder, kinds=kinds, max_depth=depth,
                                 sort_by=sort_by, descending=descending)

        def done(result):
            entries, truncated = result
            self._rescan_pending = max(0, self._rescan_pending - 1)
            current_paths = (None if self.explicit_paths is None else
                             tuple(self.explicit_paths))
            if (self._destroyed
                    or (explicit_paths is None and folder != self.folder)
                    or explicit_paths != current_paths
                    or sequence != self._list_seq):
                return
            self._list_applied = sequence
            self.entries = entries
            self._scan_truncated = truncated
            self.app.settings.update({
                "filter": self.kind_filter.get(), "sort_by": sort_by,
                "sort_desc": descending, "scan_depth": depth,
            })
            self._apply_filter(select_path=keep)
            if truncated:
                self.toast(f"Listing capped at {len(entries):,} files - "
                           f"reduce the subfolder depth or use the search box")

        def failed(exc):
            self._rescan_pending = max(0, self._rescan_pending - 1)
            if self._destroyed or sequence != self._list_seq:
                return
            self.status_var.set(f"Scan failed: {exc}")

        self.app.worker.submit(job, done, failed)

    def _match(self, name: str, terms: list[str]) -> bool:
        """Filter predicate: all terms must match (substring or glob)."""
        low = name.lower()
        for term in terms:
            if "*" in term or "?" in term:
                if not fnmatch.fnmatch(low, term):
                    return False
            elif term not in low:
                return False
        return True

    def _apply_filter(self, select_path: Optional[Path] = None) -> None:
        """Rebuild the sidebar list from ``self.entries`` and the search text."""
        terms = [t for t in self.search_var.get().strip().lower().split() if t]
        self.filtered = ([e for e in self.entries if self._match(e.relname, terms)]
                         if terms else list(self.entries))

        target = select_path or (self.current.path if self.current else None)
        size = self.thumb_size.get()

        # Only touch the Treeview when the visible set actually changed. A
        # figure being overwritten every second must not make the list flicker
        # or throw away its thumbnails.
        # A recursive listing is shown as a real tree - one collapsible node per
        # sub-folder - but only when the sort order is folder-aware. Sorting by
        # date or size is asked for precisely to see the newest files *across*
        # folders, so grouping them back up would undo the sort; those modes get
        # a flat list with the relative path on each row instead.
        grouped = (self.explicit_paths is None
                   and self.scan_depth.get() != 0
                   and self.sort_by.get() in ("name", "kind")
                   and any(entry.depth for entry in self.filtered))

        rows = [str(entry.path) for entry in self.filtered]
        if rows != self._displayed or grouped != self._grouped:
            self._suppress_select = True
            if grouped != self._grouped:
                # Only the tree style carries an expander element; the flat one
                # deliberately has none, so no empty gutter appears in front of
                # every file name when there are no folders to expand.
                self.tree.configure(style=LIST_TREE if grouped else LIST_FLAT)
            self.tree.delete(*self.tree.get_children())
            groups: dict[str, str] = {"": ""}     # sub-folder -> parent node iid
            for entry in self.filtered:
                parent = self._ensure_group(entry.subfolder, groups) if grouped else ""
                label = entry.name if grouped else entry.relname
                tags = (("doc",) if entry.kind == D.KIND_DOC else
                        ("video",) if entry.kind == D.KIND_VIDEO else ())
                self.tree.insert(parent, "end", iid=str(entry.path), text=f"  {label}",
                                 tags=tags)
            self._suppress_select = False
            self._displayed = rows
            self._grouped = grouped
            row_set = set(rows)
            self._thumb_photos = {iid: photo for iid, photo in self._thumb_photos.items()
                                  if iid in row_set}
            self._thumb_keys = {iid: key for iid, key in self._thumb_keys.items()
                                if iid in self._thumb_photos}
            for iid, photo in self._thumb_photos.items():
                try:
                    self.tree.item(iid, image=photo)
                except tk.TclError:
                    pass

        self._update_count()

        if size:
            self._queue_thumbnails(size)

        if not self.filtered:
            self._load_token = None
            if self._load_future is not None:
                self._load_future.cancel()
            self._stop_video_session()
            self.current = None
            self.document = None
            self.canvas.delete("all")
            self.pdf_controls.grid_remove()
            self._draw_empty()
            self.status_var.set(
                f"No matching files in {self.folder}"
                if self.explicit_paths is None else
                ("No opened files match the current filter"
                 if self.explicit_paths else "No files open"))
            self.page_var.set("")
            self.zoom_var.set("")
            return

        index = 0
        if target is not None:
            for position, entry in enumerate(self.filtered):
                if entry.path == target:
                    index = position
                    break
            else:
                index = 0 if self.current is None else min(self._index(), len(self.filtered) - 1)
        self.pending_select = None
        same = self.current is not None and self.filtered[index].path == self.current.path
        self._highlight(index)
        if not same:
            self.load(self.filtered[index])
        else:
            # Same file: only reload when the bytes changed, and never
            # underneath an edit session that has not been reconciled.
            fresh = self.filtered[index]
            if self.auto_reload.get() and self.current and fresh.stamp != self.current.stamp:
                decision = self.edit.reload_decision(fresh)
                if decision == "adopt":
                    self.edit.adopt_entry(fresh)
                elif decision == "reload":
                    self.load(fresh, keep_view=True, note="Reloaded")
                else:
                    self.current = fresh
            else:
                self.current = fresh

    def _update_count(self) -> None:
        """Refresh the count beside the FILES header.

        Two forms, chosen by the width the user has dragged the list to: the
        breakdown when it fits, the bare total when it does not. Tk labels do
        not ellipsize, so the alternative to shortening is text sliding under
        the header and off the edge.
        """
        total = len(self.filtered)
        if self._sidebar_px < COUNT_COMPACT_BELOW:
            count = str(total)
        else:
            docs_count = sum(1 for e in self.filtered if e.kind == D.KIND_DOC)
            videos = sum(1 for e in self.filtered if e.kind == D.KIND_VIDEO)
            images = total - docs_count - videos
            parts = []
            if images:
                parts.append(f"{images} img")
            if docs_count:
                parts.append(f"{docs_count} doc")
            if videos:
                parts.append(f"{videos} vid")
            count = " · ".join(parts) if parts else str(total)
            if self.explicit_paths is None and self.scan_depth.get() != 0:
                folders = len({e.subfolder for e in self.filtered})
                if folders > 1:
                    count += f" · {folders} dirs"
        if self._scan_truncated:
            # The toast that said so has long since faded, and a listing that
            # is not the whole truth has to keep saying so.
            count += " ⚠"
        self.count_var.set(count)
        self._auto_sidebar()
        self._sync_path_controls()

    def _auto_sidebar(self) -> None:
        """Collapse the file list for a single explicitly opened file.

        One row in a 300 px panel is not a list, it is wasted width beside
        the document. The collapse is a layout override, never a change to
        the user's Files preference: opening a folder or a second file lifts
        it, and so does toggling Files by hand.
        """
        key = (self.explicit_paths is not None, len(self.filtered))
        if key == self._auto_sidebar_key:
            return
        self._auto_sidebar_key = key
        collapse = key[0] and key[1] <= 1
        if collapse != self._sidebar_auto_collapsed:
            self._sidebar_auto_collapsed = collapse
            self._apply_layout()

    def _sync_path_controls(self) -> None:
        """Folder mode shows the editable path; explicit files show a chip."""
        if getattr(self, "path_chip", None) is None:
            return
        try:
            if self.explicit_paths is None:
                self.path_chip.grid_remove()
                self.folder_entry.grid()
                self.clear_folder_button.grid()
                self.clear_folder_button.state(["!disabled"])
                return
            self.folder_entry.grid_remove()
            self.clear_folder_button.grid_remove()
            parent = self.current.path.parent if self.current else None
            if parent is None:
                self.path_chip_var.set("Opened files")
                self.path_chip_tip.text = "Files opened directly"
            else:
                self.path_chip_var.set(parent.name or str(parent))
                self.path_chip_tip.text = f"{parent}\nClick to browse this folder"
            self.path_chip.grid()
        except tk.TclError:
            pass

    def _open_chip_folder(self) -> None:
        if self.current is None:
            self.choose_folder()
            return
        self.set_folder(self.current.path.parent, select=self.current.path)

    def _ensure_group(self, subfolder: str, groups: dict[str, str]) -> str:
        """Return the tree node for ``subfolder``, creating missing ancestors.

        Folder nodes get an iid that can never collide with a file path: real
        paths cannot contain a NUL byte, and every selection handler treats an
        iid starting with :data:`GROUP_PREFIX` as "not a file".
        """
        if subfolder in groups:
            return groups[subfolder]
        head, _, tail = subfolder.rpartition(os.sep)
        parent = self._ensure_group(head, groups) if head else ""
        iid = GROUP_PREFIX + subfolder
        self.tree.insert(parent, "end", iid=iid, text=f"  {tail or subfolder}",
                         open=subfolder not in self._collapsed, tags=("group",))
        groups[subfolder] = iid
        return iid

    def _on_group_toggle(self, _event=None) -> None:
        """Record which folders the user collapsed.

        The list is rebuilt whenever the visible set changes, which - with a
        plotting script writing into the folder - is every few seconds. Without
        this, the folder just collapsed would spring straight back open.
        """
        focused = self.tree.focus()
        if not focused.startswith(GROUP_PREFIX):
            return
        subfolder = focused[len(GROUP_PREFIX):]
        try:
            is_open = bool(self.tree.item(focused, "open"))
        except tk.TclError:
            return
        if is_open:
            self._collapsed.discard(subfolder)
        else:
            self._collapsed.add(subfolder)

    def _highlight(self, index: int) -> None:
        """Select a row without triggering a reload."""
        if not (0 <= index < len(self.filtered)):
            return
        iid = str(self.filtered[index].path)
        self._suppress_select = True
        try:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        except tk.TclError:
            pass
        self._suppress_select = False

    def _index(self) -> int:
        if self.current:
            for position, entry in enumerate(self.filtered):
                if entry.path == self.current.path:
                    return position
        return 0

    def _on_tree_select(self, _event=None) -> None:
        if self._suppress_select:
            return
        selection = self.tree.selection()
        if not selection or selection[0].startswith(GROUP_PREFIX):
            return
        path = Path(selection[0])
        if self.current and self.current.path == path:
            return
        for entry in self.filtered:
            if entry.path == path:
                self.load(entry)
                return

    # ---------------------------------------------------------- thumbnails
    def _thumb_size_changed(self) -> None:
        """Re-lay the file list after a thumbnail size or density change."""
        size = self.thumb_size.get()
        self.app.settings["thumb_size"] = size
        self.app.settings["density"] = self.density.get()
        configure_list_density(self, size, self.density.get())
        self._thumb_photos.clear()
        self._thumb_keys.clear()
        self._displayed = []          # force a rebuild so old icons disappear
        self._apply_filter()

    def _queue_thumbnails(self, size: int) -> None:
        """Fill in list thumbnails: cache hits immediately, misses in background."""
        self._thumb_generation += 1
        generation = self._thumb_generation
        # Drop queued work from the previous list instead of decoding images
        # whose results would immediately be discarded.
        for future in self._thumb_jobs:
            future.cancel()
        self._thumb_jobs = []

        def valid() -> bool:
            """Read plain Python state only; workers must never call Tk."""
            return not self._destroyed and generation == self._thumb_generation

        def apply(payload) -> None:
            if generation != self._thumb_generation or self._destroyed:
                return
            entry, image = payload
            iid = str(entry.path)
            if not self.tree.exists(iid):
                return
            photo = ImageTk.PhotoImage(image, master=self)
            self._thumb_photos[iid] = photo
            self._thumb_keys[iid] = entry.key + (size,)
            try:
                self.tree.item(iid, image=photo)
            except tk.TclError:
                pass

        def make(entry: D.Entry):
            key = entry.key + (size,)
            image = D.thumbnail_for(entry, size)
            if len(self._thumb_cache) > 600:
                self._thumb_cache.clear()
            self._thumb_cache[key] = image
            return entry, image

        video_jobs = 0
        video_placeholder: Optional[Image.Image] = None
        for entry in self.filtered[:400]:
            iid = str(entry.path)
            key = entry.key + (size,)
            if self._thumb_keys.get(iid) == key:
                continue                       # already on screen and current
            cached = self._thumb_cache.get(key)
            if cached is not None:
                apply((entry, cached))
                continue
            # Opening hundreds of movie containers merely for sidebar posters
            # can monopolise the shared worker pool. Decode a generous first
            # screenful and use a crisp media tile for the rest. Playback has
            # its own thread and is never held behind thumbnail work.
            if entry.kind == D.KIND_VIDEO and video_jobs >= 64:
                if video_placeholder is None:
                    video_placeholder = V.placeholder_thumbnail(size)
                apply((entry, video_placeholder))
                continue
            if entry.kind == D.KIND_VIDEO:
                video_jobs += 1
            self._thumb_jobs.append(self.app.worker.submit(
                lambda e=entry: make(e), apply, lambda _exc: None,
                background=True, valid=valid))

    # ------------------------------------------------------------- loading
    def load(self, entry: D.Entry, *, keep_view: bool = False, note: str = "Loaded") -> None:
        """Load a still document asynchronously or start a video stream."""
        self.current = entry
        self._update_window_title()
        self._sync_path_controls()
        # Every navigation, including video, invalidates an older still load.
        token = time.perf_counter()
        self._load_token = token
        if self._load_future is not None:
            self._load_future.cancel()
        if entry.kind == D.KIND_VIDEO:
            self._load_video(entry, keep_view=keep_view, note=note)
            return

        self._stop_video_session()
        self.pdf_controls.grid_remove()
        dpi = self.pdf_dpi.get()

        def job():
            return self.app.cache.get(entry, pdf_dpi=dpi)

        def done(document):
            if self._destroyed or getattr(self, "_load_token", None) != token:
                return
            self.document = document
            self.text.on_document_loaded()
            self.links.on_document_loaded()
            if not keep_view:
                self.page = 0
                self.rotation = 0
                self.flip_h = self.flip_v = False
                self.flip_h_var().set(False)
                self.flip_v_var().set(False)
                self.fit_mode = self._open_fit_mode(entry)
            self._minimap_key = None
            self._invalidate()
            if not keep_view:
                self.scale = self._fit_scale(self.fit_mode)
                self.off_x = self.off_y = 0.0
                self.redraw_now()
            else:
                self.redraw_now()
            self._sync_pdf_controls()
            self._update_status(note)
            self._update_info()
            # A parked edit session for this file takes over the document
            # object here, restoring dirty state and undo history.
            self.edit.on_document_loaded()
            self.index.refresh()
            self._prefetch_neighbours()

        def failed(exc):
            if self._destroyed or self._load_token != token:
                return
            self.document = None
            self.pdf_controls.grid_remove()
            self.canvas.delete("all")
            self._draw_message(str(exc))
            self.status_var.set(f"Could not open {entry.relname}: {exc}")
            self.edit.sync_toolbar()

        self._load_future = self.app.worker.submit(
            job, done, failed,
            valid=lambda: not self._destroyed and self._load_token == token)

    def _load_video(self, entry: D.Entry, *, keep_view: bool, note: str) -> None:
        """Start a streaming session without placing movie bytes in the cache."""
        previous = self._video_session.snapshot() if self._video_session is not None else None
        resume_at = previous.position if (keep_view and previous is not None) else 0.0
        autoplay = (not previous.paused if keep_view and previous is not None
                    else self.video_autoplay.get())
        self._stop_video_session()
        self.document = None
        self.pdf_controls.grid_remove()
        self.page = 0
        self._minimap_key = None
        self._video_last_state = ""
        self.canvas.delete("all")
        self._draw_message("Loading video…")
        self.video_controls.grid()
        self._adapt_video_controls(self._canvas_size()[0])
        self.video_position.set(0.0)
        self.video_time_var.set("0:00 / 0:00")
        self.video_meta_var.set("LOADING")
        session = V.VideoSession(
            entry.path, self._canvas_size(), autoplay=autoplay,
            loop=self.video_loop.get(),
        )
        self._video_session = session
        if resume_at > 0:
            session.seek(resume_at)
        self._update_status(note)
        self._video_job = self.after(15, self._poll_video)

    def _stop_video_session(self) -> None:
        """Cancel presentation, stop decoding, and release the current movie."""
        if self._video_job:
            try:
                self.after_cancel(self._video_job)
            except Exception:
                pass
            self._video_job = None
        session = self._video_session
        self._video_session = None
        if session is not None:
            session.close()
        if hasattr(self, "video_controls"):
            self.video_controls.grid_remove()
        self._video_photo = None
        try:
            self.canvas.delete("video")
        except Exception:
            pass

    def _poll_video(self) -> None:
        """Present the newest decoded frame and refresh the transport state."""
        self._video_job = None
        session = self._video_session
        if self._destroyed or session is None:
            return

        snapshot = session.snapshot()
        state_changed = snapshot.state != self._video_last_state
        frame = session.pop_frame()
        if frame is not None:
            _sequence, image, _position = frame
            self._video_photo = ImageTk.PhotoImage(image, master=self)
            self.canvas.delete("scene")
            cw, ch = self._canvas_size()
            self.canvas.create_image(
                cw / 2, ch / 2, image=self._video_photo, anchor="center",
                tags=("scene", "video"),
            )

        if snapshot.state == "error":
            if state_changed:
                self._draw_message(snapshot.error)
                self.status_var.set(
                    f"Could not play {self.current.relname}: {snapshot.error}"
                )
        else:
            duration = max(snapshot.duration, 0.001)
            self.video_scale.configure(to=duration)
            if not self._video_scrubbing:
                self.video_position.set(min(snapshot.position, duration))
            shown_position = self.video_position.get() if self._video_scrubbing else snapshot.position
            self.video_time_var.set(
                f"{V.format_clock(shown_position)} / {V.format_clock(snapshot.duration)}"
            )
            codec = snapshot.codec or "VIDEO"
            if snapshot.frame_count:
                shown_frame = min(snapshot.frame_index + 1, snapshot.frame_count)
                self.video_meta_var.set(
                    f"{codec}  {shown_frame:,}/{snapshot.frame_count:,}"
                )
            else:
                self.video_meta_var.set(codec)

        playing = snapshot.state == "playing"
        self.video_play_button.configure(
            image=self.icons.get("pause" if playing else "play"),
        )
        if state_changed:
            self._video_last_state = snapshot.state
            self._update_readouts()
            self._update_info()
        delay = 15 if snapshot.state in ("loading", "playing") else 80
        self._video_job = self.after(delay, self._poll_video)

    def toggle_video(self) -> None:
        session = self._video_session
        if session is not None:
            session.toggle()

    def stop_video_playback(self) -> None:
        session = self._video_session
        if session is not None:
            session.stop_playback()

    def seek_video(self, seconds: float) -> None:
        session = self._video_session
        if session is not None:
            session.seek_relative(seconds)

    def step_video_frames(self, frames: int) -> None:
        """Pause and move by an exact number of decoded video frames."""
        session = self._video_session
        if session is not None:
            session.step_frames(frames)

    def _video_jump_back_or_previous_file(self) -> None:
        """Use Up as the configurable reverse jump only while viewing video."""
        if self._video_session is not None:
            self.step_video_frames(-self.video_frame_step.get())
        else:
            self.previous_file()

    def _video_jump_forward_or_next_file(self) -> None:
        """Use Down as the configurable forward jump only while viewing video."""
        if self._video_session is not None:
            self.step_video_frames(self.video_frame_step.get())
        else:
            self.next_file()

    def _video_seek_press(self, _event=None) -> None:
        self._video_scrubbing = True

    def _video_seek_release(self, _event=None) -> None:
        self._video_scrubbing = False
        if self._video_session is not None:
            self._video_session.seek(self.video_position.get())

    def _video_loop_changed(self) -> None:
        self.app.settings["video_loop"] = self.video_loop.get()
        if self._video_session is not None:
            self._video_session.set_loop(self.video_loop.get())

    def _video_autoplay_changed(self) -> None:
        self.app.settings["video_autoplay"] = self.video_autoplay.get()

    def _prefetch_neighbours(self) -> None:
        """Warm the cache with the next and previous file so navigation is instant."""
        index = self._index()
        dpi = self.pdf_dpi.get()
        selected = self.current
        for offset in (1, -1):
            position = index + offset
            if 0 <= position < len(self.filtered):
                entry = self.filtered[position]
                if entry.kind == D.KIND_VIDEO:
                    continue
                if self.app.cache.peek(entry, pdf_dpi=dpi) is None:
                    self.app.worker.submit(
                        lambda e=entry: self.app.cache.get(e, pdf_dpi=dpi),
                        lambda _r: None, lambda _e: None, background=True,
                        valid=lambda: not self._destroyed and self.current is selected)

    def reload_all(self) -> None:
        """Force a rescan and a fresh decode of the current file."""
        self.app.cache.clear()
        self.rescan()
        if self.current:
            self.load(self.current, keep_view=True, note="Reloaded")

    def clear_caches(self) -> None:
        self.app.cache.clear()
        self._thumb_cache.clear()
        self.toast("Caches cleared")
        self.reload_all()

    # --------------------------------------------------------- view geometry
    def logical_size(self) -> tuple[int, int]:
        """Page size after rotation, in page pixels at 100 %."""
        if self.document is None:
            return (1, 1)
        width, height = self.document.page_size(self.page)
        return (height, width) if self.rotation % 180 else (width, height)

    def _canvas_size(self) -> tuple[int, int]:
        return max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)

    def _is_pdf(self) -> bool:
        return (self.current is not None and self.document is not None
                and self.current.path.suffix.lower() == ".pdf")

    def _fit_scale(self, mode: str) -> float:
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        margin = 16
        if mode == FIT_READING and self._is_pdf():
            base_dpi = float(getattr(self.document, "base_dpi", self.pdf_dpi.get()))
            return pdf_reading_scale(width, base_dpi, cw)
        if mode == FIT_OPTIMAL:
            if self._is_pdf():
                base_dpi = float(getattr(self.document, "base_dpi", self.pdf_dpi.get()))
                cap = PDF_OPTIMAL_MAX_ZOOM * PDF_SCREEN_DPI / base_dpi
            else:
                cap = IMAGE_OPTIMAL_MAX_SCALE
            return optimal_scale(width, cw, cap, margin)
        if mode == FIT_WIDTH:
            return max((cw - margin) / width, ZOOM_MIN)
        return max(min((cw - margin) / width, (ch - margin) / height), ZOOM_MIN)

    def _clamp_offsets(self) -> None:
        """Centre small pages and keep large ones inside their bounds."""
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        view_w, view_h = cw / self.scale, ch / self.scale
        if width <= view_w:
            self.off_x = -(view_w - width) / 2
        else:
            self.off_x = min(max(self.off_x, 0.0), width - view_w)
        if height <= view_h:
            self.off_y = -(view_h - height) / 2
        else:
            self.off_y = min(max(self.off_y, 0.0), height - view_h)

    def fit_page(self) -> None:
        self.fit_mode = FIT_PAGE
        self.scale = self._fit_scale(FIT_PAGE)
        self._invalidate()
        self.redraw_now()

    def fit_width(self) -> None:
        self.fit_mode = FIT_WIDTH
        self.scale = self._fit_scale(FIT_WIDTH)
        self.off_y = 0.0
        self._invalidate()
        self.redraw_now()

    def optimal_zoom(self) -> None:
        """Fill the canvas width, capped (see :func:`optimal_scale`)."""
        if self.document is None:
            return
        self.fit_mode = FIT_OPTIMAL
        self.scale = self._fit_scale(FIT_OPTIMAL)
        self.off_y = 0.0
        self._invalidate()
        self.redraw_now()

    def _open_fit_mode(self, entry: D.Entry) -> str:
        """The fit mode a freshly opened file starts in (View > Zoom on Open)."""
        choice = str(self.app.settings.get("open_fit", "optimal"))
        is_pdf = entry.path.suffix.lower() == ".pdf"
        for value, _label, pdf_mode, image_mode in OPEN_FIT_CHOICES:
            if value == choice:
                return pdf_mode if is_pdf else image_mode
        return FIT_OPTIMAL

    def _open_fit_changed(self) -> None:
        """Persist the choice and apply it to the file on screen at once."""
        self.app.settings["open_fit"] = self.open_fit.get()
        if self.current is None or self.document is None:
            return
        self.fit_mode = self._open_fit_mode(self.current)
        self.scale = self._fit_scale(self.fit_mode)
        self.off_x = self.off_y = 0.0
        self._invalidate()
        self.redraw_now()

    def actual_size(self) -> None:
        if self._is_pdf():
            base_dpi = float(getattr(self.document, "base_dpi", self.pdf_dpi.get()))
            self.zoom_to(PDF_SCREEN_DPI / base_dpi)
        else:
            self.zoom_to(1.0)

    def zoom_by(self, factor: float, focus: Optional[tuple[float, float]] = None) -> None:
        self.zoom_to(self.scale * factor, focus)

    def zoom_to(self, scale: float, focus: Optional[tuple[float, float]] = None) -> None:
        """Set the zoom level, keeping ``focus`` (canvas point) anchored."""
        if self.document is None:
            return
        new_scale = min(max(scale, ZOOM_MIN), ZOOM_MAX)
        cw, ch = self._canvas_size()
        cx, cy = focus if focus else (cw / 2, ch / 2)
        lx = self.off_x + cx / self.scale
        ly = self.off_y + cy / self.scale
        self.scale = new_scale
        self.off_x = lx - cx / new_scale
        self.off_y = ly - cy / new_scale
        self.fit_mode = FIT_FREE
        self._invalidate()
        self.redraw_now()

    def rotate(self, degrees: int) -> None:
        self.rotation = (self.rotation + degrees) % 360
        if self.fit_mode != FIT_FREE:
            self.scale = self._fit_scale(self.fit_mode)
        self._minimap_key = None
        self._invalidate()
        self.redraw_now()

    def _pan_by(self, dx: float, dy: float) -> None:
        self.off_x += dx / self.scale
        self.off_y += dy / self.scale
        self.fit_mode = FIT_FREE if self.fit_mode == FIT_PAGE else self.fit_mode
        self._invalidate()
        self.redraw_now()

    # ------------------------------------------------------- transformations
    def _logical_box_to_source(self, box: tuple[float, float, float, float]
                               ) -> tuple[float, float, float, float]:
        """Invert rotation and flips: logical (screen) box -> source page box."""
        assert self.document is not None
        width, height = self.document.page_size(self.page)
        lw, lh = (height, width) if self.rotation % 180 else (width, height)
        x0, y0, x1, y1 = box
        if self.flip_h:
            x0, x1 = lw - x1, lw - x0
        if self.flip_v:
            y0, y1 = lh - y1, lh - y0
        rotation = self.rotation % 360
        if rotation == 0:
            return (x0, y0, x1, y1)
        if rotation == 90:
            return (y0, height - x1, y1, height - x0)
        if rotation == 180:
            return (width - x1, height - y1, width - x0, height - y0)
        return (width - y1, x0, width - y0, x1)

    def _orient(self, image: Image.Image) -> Image.Image:
        """Apply rotation and flips to a rendered source tile."""
        rotation = self.rotation % 360
        if rotation == 90:
            image = image.transpose(Image.Transpose.ROTATE_270)
        elif rotation == 180:
            image = image.transpose(Image.Transpose.ROTATE_180)
        elif rotation == 270:
            image = image.transpose(Image.Transpose.ROTATE_90)
        if self.flip_h:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if self.flip_v:
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        return image

    def _flatten(self, image: Image.Image) -> Image.Image:
        """Composite alpha onto the chosen backdrop and apply colour effects."""
        if image.mode in ("RGBA", "LA", "PA"):
            mode = self.transparency.get()
            rgba = image.convert("RGBA")
            if mode == "checker":
                background = _checker_background(rgba.size)
            else:
                colour = "#ffffff" if mode == "white" else self.palette.canvas
                background = Image.new("RGB", rgba.size, colour)
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        if self.grayscale.get():
            image = ImageOps.grayscale(image)
        if self.invert.get():
            image = ImageOps.invert(image.convert("RGB"))
        return image

    # ------------------------------------------------------------- rendering
    def _invalidate(self) -> None:
        """Bump the render generation so stale async results are discarded."""
        self._generation += 1

    def _tile_request(self) -> Optional[tuple]:
        """Compute the visible region and the pixel size it must be drawn at."""
        if self.document is None:
            return None
        self._clamp_offsets()
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        left = max(self.off_x, 0.0)
        top = max(self.off_y, 0.0)
        right = min(self.off_x + cw / self.scale, float(width))
        bottom = min(self.off_y + ch / self.scale, float(height))
        if right - left < 0.5 or bottom - top < 0.5:
            return None
        out_w = max(1, int(round((right - left) * self.scale)))
        out_h = max(1, int(round((bottom - top) * self.scale)))
        canvas_x = int(round((left - self.off_x) * self.scale))
        canvas_y = int(round((top - self.off_y) * self.scale))
        return (left, top, right, bottom, out_w, out_h, canvas_x, canvas_y)

    def _render_tile(self, request: tuple, fast: bool) -> Image.Image:
        """Rasterise the visible region (runs on either thread)."""
        assert self.document is not None
        left, top, right, bottom, out_w, out_h, _, _ = request
        source_box = self._logical_box_to_source((left, top, right, bottom))
        swap = bool(self.rotation % 180)
        target = (out_h, out_w) if swap else (out_w, out_h)
        if fast and self.document.kind == D.KIND_DOC:
            # Rasterising a PDF is the expensive part: do it at 60 % and let the
            # cheap upscale carry the interactive frame.
            target = (max(1, int(target[0] * 0.6)), max(1, int(target[1] * 0.6)))
        tile = self.document.render_region(self.page, source_box, target,
                                           fast=fast or not self.smooth.get())
        tile = self._orient(tile)
        if tile.size != (out_w, out_h):
            tile = tile.resize((out_w, out_h), Image.Resampling.BILINEAR)
        return self._flatten(tile)

    def redraw_now(self, fast: bool = True) -> None:
        """Immediate repaint, then schedule the high-quality pass."""
        if self._destroyed:
            return
        if self._video_session is not None:
            self._video_session.set_viewport(self._canvas_size())
            return
        if self.document is None:
            self._draw_empty()
            return
        request = self._tile_request()
        if request is None:
            self.canvas.delete("scene")
            return
        try:
            tile = self._render_tile(request, fast=fast)
        except Exception as exc:
            self._draw_message(f"Render failed: {exc}")
            return
        self._paint(tile, request)
        self._update_readouts()
        if fast:
            self._schedule_quality()

    def _schedule_quality(self) -> None:
        """After the gesture settles, recompute the tile at full quality."""
        if self._quality_job:
            try:
                self.after_cancel(self._quality_job)
            except Exception:
                pass
        self._quality_job = self.after(QUALITY_DELAY_MS, self._quality_pass)

    def _quality_pass(self) -> None:
        self._quality_job = None
        if self.document is None or self._destroyed or not self.smooth.get():
            return
        request = self._tile_request()
        if request is None:
            return
        generation = self._generation
        document = self.document

        def job():
            return self._render_tile(request, fast=False)

        def done(tile):
            if self._destroyed or generation != self._generation or self.document is not document:
                return
            self._paint(tile, request)
            self._draw_minimap()

        self.app.worker.submit(job, done, lambda _e: None)

    def _paint(self, tile: Image.Image, request: tuple) -> None:
        """Put a rendered tile on the canvas together with its drop shadow."""
        _, _, _, _, out_w, out_h, canvas_x, canvas_y = request
        photo = ImageTk.PhotoImage(tile, master=self)
        canvas = self.canvas
        canvas.delete("scene")
        p = self.palette
        # Soft shadow: three offset rectangles, each blended a little further
        # towards the backdrop, approximate a blur for the cost of three fills.
        for offset, weight in ((7, 0.30), (5, 0.55), (3, 0.85)):
            canvas.create_rectangle(canvas_x + offset, canvas_y + offset,
                                    canvas_x + out_w + offset, canvas_y + out_h + offset,
                                    fill=_mix(p.shadow, p.canvas, weight), outline="", tags="scene")
        canvas.create_image(canvas_x, canvas_y, image=photo, anchor="nw", tags="scene")
        canvas.create_rectangle(canvas_x, canvas_y, canvas_x + out_w, canvas_y + out_h,
                                outline=p.border, width=1, tags="scene")
        canvas.tag_lower("scene")
        self._photo = photo
        self._draw_minimap()
        self.edit.draw_overlay()
        self.text.draw()
        self.links.draw()

    def _draw_empty(self) -> None:
        if self.explicit_paths is None:
            message = ("No files match the current filter."
                       if self.entries else f"Nothing to show in\n{self.folder}")
        elif self.explicit_paths:
            message = "No opened files match the current filter."
        else:
            message = "Open a file (Ctrl+Shift+O) or choose a folder (Ctrl+O)."
        self._draw_message(message)

    def _draw_message(self, message: str) -> None:
        canvas = self.canvas
        canvas.delete("scene")
        cw, ch = self._canvas_size()
        canvas.create_text(cw / 2, ch / 2, text=message, fill=self.palette.muted,
                           font="FVtitle", justify="center", tags="scene")

    # --------------------------------------------------------------- minimap
    def _draw_minimap(self) -> None:
        """Overview thumbnail with the current viewport outlined."""
        canvas = self.canvas
        canvas.delete("mini")
        self._minimap_rect = None
        if not self.show_minimap.get() or self.document is None:
            return
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        if width * self.scale <= cw + 2 and height * self.scale <= ch + 2:
            return          # everything is visible; a map would be noise

        key = (id(self.document), self.page, self.rotation, self.flip_h, self.flip_v)
        if key != self._minimap_key:
            self._minimap_key = key
            try:
                factor = min(MINIMAP_MAX / width, MINIMAP_MAX / height)
                box = self._logical_box_to_source((0, 0, width, height))
                swap = bool(self.rotation % 180)
                size = (max(1, int(width * factor)), max(1, int(height * factor)))
                target = (size[1], size[0]) if swap else size
                tile = self._orient(self.document.render_region(self.page, box, target, fast=True))
                self._minimap_photo = ImageTk.PhotoImage(self._flatten(tile), master=self)
            except Exception:
                self._minimap_photo = None
        photo = self._minimap_photo
        if photo is None:
            return
        pad = 14
        x0 = cw - photo.width() - pad
        y0 = pad
        p = self.palette
        canvas.create_rectangle(x0 - 4, y0 - 4, x0 + photo.width() + 4, y0 + photo.height() + 4,
                                fill=p.panel, outline=p.border, tags="mini")
        canvas.create_image(x0, y0, image=photo, anchor="nw", tags="mini")
        sx = photo.width() / width
        sy = photo.height() / height
        vx0 = x0 + max(self.off_x, 0) * sx
        vy0 = y0 + max(self.off_y, 0) * sy
        vx1 = x0 + min(self.off_x + cw / self.scale, width) * sx
        vy1 = y0 + min(self.off_y + ch / self.scale, height) * sy
        canvas.create_rectangle(vx0, vy0, vx1, vy1, outline=p.accent, width=2, tags="mini")
        self._minimap_rect = (x0, y0, photo.width(), photo.height())
        # The map answers "where am I?" right after the view moved; once the
        # pointer has been idle it is just a sticker on the page, so it
        # fades and comes back with the next pan, zoom or scroll.
        if self._minimap_job:
            try:
                self.after_cancel(self._minimap_job)
            except Exception:
                pass
        self._minimap_job = self.after(MINIMAP_FADE_MS, self._fade_minimap)

    def _fade_minimap(self) -> None:
        self._minimap_job = None
        if self._destroyed:
            return
        try:
            self.canvas.delete("mini")
        except tk.TclError:
            return
        self._minimap_rect = None

    # ---------------------------------------------------------------- events
    def _on_canvas_configure(self, _event=None) -> None:
        """Debounced resize: refit when in a fit mode, else just repaint."""
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass

        def apply():
            self._resize_job = None
            if self._video_session is not None:
                self._adapt_video_controls(self._canvas_size()[0])
                self._video_session.set_viewport(self._canvas_size())
                return
            if self.document is None:
                self._draw_empty()
                return
            if self.fit_mode in FIT_MODES_TRACKING:
                self.scale = self._fit_scale(self.fit_mode)
            self._invalidate()
            self.redraw_now()

        self._resize_job = self.after(40, apply)

    def _on_zoom_wheel(self, event) -> str | None:
        if self.document is None:
            return None
        factor = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        self.zoom_by(factor, focus=(event.x, event.y))
        return "break"

    def _on_wheel(self, event) -> str | None:
        if self.document is None:
            return None
        if not self._is_pdf():
            return self._on_zoom_wheel(event)

        delta = int(event.delta)
        if delta == 0:
            return "break"

        direction = -1 if delta > 0 else 1
        _width, height = self.logical_size()
        _cw, canvas_height = self._canvas_size()
        maximum_y = max(0.0, height - canvas_height / self.scale)
        edge_epsilon = max(1.0, 2.0 / self.scale)
        step_pixels = min(180.0, max(24.0, abs(delta) * 0.8))

        # Consume the wheel inside an oversized page first. Reaching the edge
        # does not change page until the user continues scrolling.
        if direction > 0 and maximum_y > edge_epsilon and self.off_y < maximum_y - edge_epsilon:
            self.off_y = min(maximum_y, max(0.0, self.off_y) + step_pixels / self.scale)
            self._invalidate()
            self.redraw_now()
            return "break"
        if direction < 0 and maximum_y > edge_epsilon and self.off_y > edge_epsilon:
            self.off_y = max(0.0, self.off_y - step_pixels / self.scale)
            self._invalidate()
            self.redraw_now()
            return "break"

        # While editing, the wheel never crosses a page boundary: a drawing
        # gesture must not flip the page underneath itself. PageUp/Down and
        # the page bar still navigate.
        if self.edit.wheel_scrolls():
            return "break"

        now = time.perf_counter()
        if now - self._last_pdf_page_at < PDF_WHEEL_COOLDOWN_S:
            return "break"
        self._last_pdf_page_at = now
        self.change_pdf_page(direction, land_at_end=direction < 0)
        return "break"

    def _on_press(self, event) -> None:
        self.canvas.focus_set()
        if self._minimap_click(event):
            return
        # The edit controller gets the event before snip and pan, exactly the
        # snip idiom: a handled press must never also set a pan anchor.
        if self.edit.on_press(event):
            return
        if self.snip_mode.get():
            self._snip_start = (event.x, event.y)
            self._snip_rect = None
            return
        # A link under the press is remembered (not consumed): only a
        # release without drag on the same link opens it, in _on_release.
        self.links.on_press(event)
        # A press on text starts a selection; on blank page it falls
        # through to panning, so the reading gesture is unchanged.
        if self.text.on_press(event):
            return
        self._pan_anchor = (event.x, event.y, self.off_x, self.off_y)
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, event) -> None:
        if self.edit.on_drag(event):
            return
        if self._snip_start is not None:
            self._update_snip_overlay(event.x, event.y, shift=bool(event.state & 0x0001))
            return
        if self.text.on_drag(event):
            return
        if self._pan_anchor is None:
            return
        x0, y0, ox, oy = self._pan_anchor
        self.off_x = ox - (event.x - x0) / self.scale
        self.off_y = oy - (event.y - y0) / self.scale
        self.fit_mode = FIT_FREE
        self._invalidate()
        self.redraw_now()

    def _on_release(self, event) -> None:
        if self.edit.on_release(event):
            return
        if self._snip_start is not None:
            self._finish_snip(event.x, event.y)
            return
        clicked = self.links.on_release(event)
        if not self.text.on_release(event):
            self._end_pan()
        if clicked is not None:
            self.links.open(clicked)

    def _end_pan(self) -> None:
        self._pan_anchor = None
        self.canvas.configure(cursor="crosshair" if self.snip_mode.get() else "")
        self.text.cursor_reset()

    def _on_middle_press(self, event) -> None:
        self._pan_anchor = (event.x, event.y, self.off_x, self.off_y)
        self.canvas.configure(cursor="fleur")

    def _on_middle_drag(self, event) -> None:
        if self._pan_anchor:
            x0, y0, ox, oy = self._pan_anchor
            self.off_x = ox - (event.x - x0) / self.scale
            self.off_y = oy - (event.y - y0) / self.scale
            self.fit_mode = FIT_FREE
            self._invalidate()
            self.redraw_now()

    def _on_double_click(self, _event) -> None:
        """Toggle between fit-page and 100 %."""
        if self._video_session is not None:
            self.toggle_video()
            return
        if self.edit.commit_placement():
            return
        if self.edit.on_double_click(_event):
            return
        if self.text.select_word_at(_event):
            return
        if abs(self.scale - 1.0) < 1e-6:
            self.fit_page()
        else:
            self.actual_size()

    def _on_triple_click(self, event) -> None:
        """Triple click selects a whole line of text; inert elsewhere."""
        self.text.select_line_at(event)

    def _on_motion(self, event) -> None:
        # A link wins the cursor; otherwise the text layer decides.
        if not self.links.on_motion(event):
            self.text.on_motion(event)
        if self.document is None:
            self._set_probe("")
            return
        lx = self.off_x + event.x / self.scale
        ly = self.off_y + event.y / self.scale
        width, height = self.logical_size()
        if not (0 <= lx < width and 0 <= ly < height):
            self._set_probe("")
            return
        box = self._logical_box_to_source((lx, ly, lx + 1, ly + 1))
        sx, sy = int(box[0]), int(box[1])
        text = f"{sx:>6}, {sy:<6}"
        colour = ""
        sample = self.document.pixel(self.page, sx, sy)
        if sample:
            if len(sample) >= 3:
                colour = "#%02x%02x%02x" % sample[:3]
            else:
                value = sample[0]
                colour = "#%02x%02x%02x" % (value, value, value)
            text += f"  {colour.upper()}"
        self._set_probe(text, colour)

    def _set_probe(self, text: str, colour: str = "") -> None:
        if text == self._probe_text:
            return
        self._probe_text = text
        self.probe_var.set(text)
        try:
            if colour:
                self.swatch.configure(background=colour)
                self.swatch.grid()
            else:
                self.swatch.grid_remove()
        except tk.TclError:
            pass

    def _minimap_click(self, event) -> bool:
        """Clicking the overview map jumps the viewport there."""
        if not self._minimap_rect:
            return False
        x0, y0, mw, mh = self._minimap_rect
        if not (x0 <= event.x <= x0 + mw and y0 <= event.y <= y0 + mh):
            return False
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        self.off_x = (event.x - x0) / mw * width - cw / self.scale / 2
        self.off_y = (event.y - y0) / mh * height - ch / self.scale / 2
        self.fit_mode = FIT_FREE
        self._invalidate()
        self.redraw_now()
        return True

    def _on_escape(self, _event=None) -> None:
        if self.edit.review.cancel_mode():
            return
        if self.attributes("-fullscreen"):
            self.attributes("-fullscreen", False)
            return
        if self.edit.on_escape():
            return
        if self.text.clear_selection():
            return
        if self.snip_mode.get() or self._snip_start is not None:
            self._snip_start = None
            self.canvas.delete("snip")
            self.snip_mode.set(False)
            self._snip_mode_changed()
            return
        if self.search_var.get():
            self.search_var.set("")

    # ------------------------------------------------------------- snipping
    def _snip_mode_changed(self) -> None:
        active = self.snip_mode.get()
        if active and self._video_session is not None:
            self.snip_mode.set(False)
            self.toast("Video frames can be copied or saved, but region snipping is for still images")
            return
        self.canvas.configure(cursor="crosshair" if active else "")
        self.text.cursor_reset()
        if active:
            self.text.clear_selection()
            self.toast("Snip: drag a rectangle to copy it. Shift = square, Esc = cancel.")
        else:
            self.canvas.delete("snip")
            self._snip_start = None

    def _update_snip_overlay(self, x: float, y: float, shift: bool = False) -> None:
        """Draw the dimmed surround, the selection outline and a live size label."""
        if self._snip_start is None:
            return
        x0, y0 = self._snip_start
        if shift:
            side = max(abs(x - x0), abs(y - y0))
            x = x0 + side * (1 if x >= x0 else -1)
            y = y0 + side * (1 if y >= y0 else -1)
        left, right = sorted((x0, x))
        top, bottom = sorted((y0, y))
        self._snip_rect = (left, top, right, bottom)

        canvas = self.canvas
        canvas.delete("snip")
        cw, ch = self._canvas_size()
        p = self.palette
        dim = p.shadow
        # Four rectangles around the selection, drawn with a stipple so the
        # image stays visible underneath (Tk canvas has no alpha fills).
        for coords in ((0, 0, cw, top), (0, bottom, cw, ch),
                       (0, top, left, bottom), (right, top, cw, bottom)):
            canvas.create_rectangle(*coords, fill=dim, outline="", stipple="gray50", tags="snip")
        canvas.create_rectangle(left, top, right, bottom, outline=p.accent, width=2, tags="snip")
        for cx, cy in ((left, top), (right, top), (left, bottom), (right, bottom)):
            canvas.create_rectangle(cx - 3, cy - 3, cx + 3, cy + 3,
                                    fill=p.accent, outline="", tags="snip")
        pixels_w = int(round((right - left) / self.scale))
        pixels_h = int(round((bottom - top) / self.scale))
        label_y = top - 12 if top > 24 else bottom + 14
        canvas.create_rectangle(left, label_y - 9, left + 108, label_y + 9,
                                fill=p.panel, outline=p.accent, tags="snip")
        canvas.create_text(left + 54, label_y, text=f"{pixels_w} x {pixels_h} px",
                           fill=p.text, font="FVmono", tags="snip")

    def _finish_snip(self, x: float, y: float) -> None:
        """Crop the selected region at full resolution and copy it."""
        start = self._snip_start
        self._snip_start = None
        if start is None or self.document is None:
            return
        rect = self._snip_rect or (start[0], start[1], x, y)
        left, top, right, bottom = rect
        self.canvas.delete("snip")
        if right - left < 4 or bottom - top < 4:
            self.toast("Selection too small")
            return

        # Canvas -> logical -> source, then render at export resolution.
        l0 = self.off_x + left / self.scale
        t0 = self.off_y + top / self.scale
        l1 = self.off_x + right / self.scale
        t1 = self.off_y + bottom / self.scale
        width, height = self.logical_size()
        l0, l1 = max(0.0, l0), min(float(width), l1)
        t0, t1 = max(0.0, t0), min(float(height), t1)
        if l1 - l0 < 1 or t1 - t0 < 1:
            self.toast("Selection is outside the page")
            return

        quality = 1.0
        if self.document.kind == D.KIND_DOC:
            quality = max(1.0, self.export_dpi.get() / max(self.pdf_dpi.get(), 1.0))
        out_w = max(1, int(round((l1 - l0) * quality)))
        out_h = max(1, int(round((t1 - t0) * quality)))
        source_box = self._logical_box_to_source((l0, t0, l1, t1))
        swap = bool(self.rotation % 180)
        target = (out_h, out_w) if swap else (out_w, out_h)
        try:
            tile = self._orient(self.document.render_region(self.page, source_box, target, fast=False))
            tile = self._flatten(tile)
        except Exception as exc:
            messagebox.showerror("Snip failed", str(exc), parent=self)
            return

        self.last_snip = tile
        try:
            winclip.copy_image(tile, dpi=self._effective_dpi())
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self)
            return
        self.toast(f"Copied {tile.width} x {tile.height} px to the clipboard")
        if not self.app.settings.get("snip_sticky", False):
            self.snip_mode.set(False)
            self._snip_mode_changed()

    def _effective_dpi(self) -> Optional[tuple[float, float]]:
        if self.document is None:
            return None
        if self.document.kind == D.KIND_DOC:
            value = self.export_dpi.get()
            return (value, value)
        return getattr(self.document, "dpi", None)

    # ------------------------------------------------------------- commands
    def _copy_shortcut(self, _event=None):
        """Ctrl+C copies the selected text, else the image, unless a text
        field has focus."""
        if self._typing():
            return None
        if self.text.copy_selection():
            return "break"
        self.copy_image()
        return "break"

    def copy_image(self) -> None:
        """Copy the whole current page or the current full-resolution video frame."""
        if self._video_session is not None:
            image = self._video_session.current_frame()
            if image is None:
                self.toast("The first video frame is still loading")
                return
            try:
                winclip.copy_image(image)
            except Exception as exc:
                messagebox.showerror("Clipboard error", str(exc), parent=self)
                return
            self.toast(f"Copied video frame ({image.width} x {image.height} px)")
            return
        if self.document is None:
            return
        quality = 1.0
        if self.document.kind == D.KIND_DOC:
            quality = max(1.0, self.export_dpi.get() / max(self.pdf_dpi.get(), 1.0))
        try:
            image = self._flatten(self._orient(self.document.full_page(self.page, quality_scale=quality)))
            winclip.copy_image(image, dpi=self._effective_dpi())
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self)
            return
        self.toast(f"Copied {image.width} x {image.height} px")

    def copy_view(self) -> None:
        """Copy exactly what is visible on screen, at screen resolution."""
        if self._video_session is not None:
            # The streaming renderer retains only one full-resolution source
            # frame. Copying it is preferable to reading pixels back from Tk,
            # which would be slower and lower quality.
            self.copy_image()
            return
        request = self._tile_request()
        if request is None or self.document is None:
            return
        try:
            tile = self._render_tile(request, fast=False)
            winclip.copy_image(tile)
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self)
            return
        self.toast(f"Copied the visible view ({tile.width} x {tile.height} px)")

    def copy_last_snip(self) -> None:
        if self.last_snip is None:
            self.toast("No snip yet - press S and drag a rectangle")
            return
        try:
            winclip.copy_image(self.last_snip, dpi=self._effective_dpi())
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self)
            return
        self.toast("Last snip copied again")

    def copy_path(self) -> None:
        if not self.current:
            return
        winclip.copy_text(str(self.current.path))
        self.toast("Path copied")

    def copy_folder_path(self) -> None:
        folder = self.current.path.parent if self.current else self.folder
        winclip.copy_text(str(folder))
        self.toast("Folder path copied")

    def copy_file_object(self) -> None:
        if not self.current:
            return
        try:
            winclip.copy_files([self.current.path])
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self)
            return
        self.toast("File copied - paste it into Explorer or an e-mail")

    def copy_page_text(self) -> None:
        """All text on the page: the PDF text layer or an image's OCR."""
        self.text.copy_page_text()

    def save_page_as(self) -> None:
        """Write the current still page or video frame to a new image file."""
        if self.current is None:
            return
        if self._video_session is not None:
            image = self._video_session.current_frame()
            if image is None:
                self.toast("The first video frame is still loading")
                return
            seconds = int(self._video_session.snapshot().position)
            target = filedialog.asksaveasfilename(
                parent=self, title="Save video frame as", initialdir=str(self.folder),
                initialfile=f"{self.current.path.stem}_frame_{seconds:06d}.png",
                defaultextension=".png",
                filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg")],
            )
            if target:
                self._write_image(image, Path(target))
            return
        if self.document is None:
            return
        default = f"{self.current.path.stem}"
        if self.document.page_count > 1:
            default += f"_p{self.page + 1}"
        target = filedialog.asksaveasfilename(
            parent=self, title="Save page as", initialdir=str(self.folder),
            initialfile=default + ".png", defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg"),
                       ("TIFF image", "*.tif"), ("WebP image", "*.webp")])
        if not target:
            return
        quality = 1.0
        if self.document.kind == D.KIND_DOC:
            quality = max(1.0, self.export_dpi.get() / max(self.pdf_dpi.get(), 1.0))
        image = self._flatten(self._orient(self.document.full_page(self.page, quality_scale=quality)))
        self._write_image(image, Path(target))

    def save_snip_as(self) -> None:
        if self.last_snip is None:
            self.toast("No snip yet - press S and drag a rectangle")
            return
        stem = self.current.path.stem if self.current else "snip"
        target = filedialog.asksaveasfilename(
            parent=self, title="Save snip as", initialdir=str(self.folder),
            initialfile=f"{stem}_snip.png", defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg")])
        if target:
            self._write_image(self.last_snip, Path(target))

    def export_all_pages(self) -> None:
        """Export every page of a multi-page document as numbered PNGs."""
        if self.document is None or self.current is None:
            return
        if self.document.page_count < 2:
            self.toast("This document has a single page - use Save Page As")
            return
        folder = filedialog.askdirectory(parent=self, title="Export all pages to folder",
                                         initialdir=str(self.folder))
        if not folder:
            return
        out_dir = Path(folder)
        stem = self.current.path.stem
        quality = 1.0
        if self.document.kind == D.KIND_DOC:
            quality = max(1.0, self.export_dpi.get() / max(self.pdf_dpi.get(), 1.0))
        document = self.document
        count = document.page_count

        def job():
            written = 0
            for index in range(count):
                image = document.full_page(index, quality_scale=quality)
                image = image.convert("RGB") if image.mode not in ("RGB", "RGBA", "L") else image
                image.save(out_dir / f"{stem}_p{index + 1:03d}.png")
                written += 1
            return written

        self.status_var.set(f"Exporting {count} pages…")
        self.app.worker.submit(job,
                               lambda n: self.toast(f"Exported {n} pages to {out_dir}"),
                               lambda exc: messagebox.showerror("Export failed", str(exc), parent=self))

    def _write_image(self, image: Image.Image, target: Path) -> None:
        try:
            payload = image
            if target.suffix.lower() in (".jpg", ".jpeg") and payload.mode not in ("RGB", "L"):
                payload = payload.convert("RGB")
            dpi = self._effective_dpi()
            kwargs = {"dpi": dpi} if dpi else {}
            payload.save(target, **kwargs)
        except Exception as exc:
            messagebox.showerror("Could not save", f"{target}\n\n{exc}", parent=self)
            return
        self.toast(f"Saved {target.name}")

    def open_default_app(self) -> None:
        if self.current:
            _open_in_shell(self.current.path)

    def reveal_in_explorer(self) -> None:
        if self.current:
            _reveal_in_explorer(self.current.path)
        elif self.explicit_paths is None:
            _open_in_shell(self.folder)

    def delete_current(self) -> None:
        """Send the current file to the Recycle Bin (with confirmation)."""
        if not self.current:
            return
        path = self.current.path
        if not messagebox.askyesno("Delete file",
                                   f"Move this file to the Recycle Bin?\n\n{path}",
                                   parent=self, default="no"):
            return
        self._stop_video_session()
        try:
            self._recycle(path)
        except Exception as exc:
            messagebox.showerror("Could not delete", f"{path}\n\n{exc}", parent=self)
            return
        index = self._index()
        if self.explicit_paths is not None:
            self.explicit_paths = [item for item in self.explicit_paths if item != path]
        self.current = None
        self.rescan(keep_selection=False)
        self.after(200, lambda: self.select_index(min(index, len(self.filtered) - 1)))
        self.toast(f"Deleted {path.name}")

    @staticmethod
    def _recycle(path: Path) -> None:
        """Use the Shell file operation API so the file lands in the bin."""
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT, False, None, None)
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            raise OSError(f"SHFileOperation returned {result}")

    # ------------------------------------------------------------ navigation
    def select_index(self, index: int) -> None:
        if not self.filtered:
            return
        index = max(0, min(index, len(self.filtered) - 1))
        self._highlight(index)
        entry = self.filtered[index]
        if not self.current or entry.path != self.current.path:
            self.load(entry)

    def next_file(self) -> None:
        if self.filtered:
            self.select_index((self._index() + 1) % len(self.filtered))

    def previous_file(self) -> None:
        if self.filtered:
            self.select_index((self._index() - 1) % len(self.filtered))

    def _previous_action(self) -> None:
        """Step one video frame back, otherwise navigate to the previous file."""
        if self._video_session is not None:
            self.step_video_frames(-1)
        else:
            self.previous_file()

    def _next_action(self) -> None:
        if self._video_session is not None:
            self.step_video_frames(1)
        else:
            self.next_file()

    def change_page(self, delta: int) -> None:
        """Move within a multi-page document; roll over to the next file."""
        if self.document is None:
            return
        if self.document.page_count == 1:
            self.next_file() if delta > 0 else self.previous_file()
            return
        page = self.page + delta
        if page < 0:
            self.previous_file()
            return
        if page >= self.document.page_count:
            self.next_file()
            return
        self._set_document_page(page)

    def change_pdf_page(self, delta: int, *, land_at_end: bool = False) -> None:
        """Move within a PDF without rolling into an adjacent file."""
        if not self._is_pdf() or self.document is None:
            return
        page = min(max(self.page + delta, 0), self.document.page_count - 1)
        if page != self.page:
            self._set_document_page(page, land_at_end=land_at_end)

    def _set_document_page(self, page: int, *, land_at_end: bool = False) -> None:
        """Apply one validated page index and refresh all page-specific UI."""
        self.edit.on_page_changed()
        self.text.on_page_changed()
        self.links.on_page_changed()
        self.page = page
        self._minimap_key = None
        if self.fit_mode != FIT_FREE:
            self.scale = self._fit_scale(self.fit_mode)
        self.off_x = 0.0
        if land_at_end:
            _width, height = self.logical_size()
            _cw, canvas_height = self._canvas_size()
            self.off_y = max(0.0, height - canvas_height / self.scale)
        else:
            self.off_y = 0.0
        self._invalidate()
        self.redraw_now()
        self._update_status("Page")
        self._update_info()
        self._sync_pdf_controls()
        self.index.sync_page(page)

    def _sync_pdf_controls(self) -> None:
        """Show page buttons and detected format only for multi-page PDFs."""
        if not self._is_pdf() or self.document is None or self.document.page_count < 2:
            self.pdf_controls.grid_remove()
            self.pdf_page_var.set("")
            return
        page_format = "PDF"
        if hasattr(self.document, "page_format"):
            try:
                page_format = self.document.page_format(self.page)
            except Exception:
                pass
        self.pdf_page_var.set(
            f"Page {self.page + 1} / {self.document.page_count}  ·  {page_format}")
        self.pdf_previous_button.state(
            ["!disabled"] if self.page > 0 else ["disabled"])
        self.pdf_next_button.state(
            ["!disabled"] if self.page + 1 < self.document.page_count else ["disabled"])
        self.pdf_controls.grid()

    def goto_page(self) -> None:
        if self.document is None or self.document.page_count < 2:
            return
        from tkinter import simpledialog

        page = simpledialog.askinteger(
            "Go to page", f"Page number (1 - {self.document.page_count}):",
            parent=self, minvalue=1, maxvalue=self.document.page_count, initialvalue=self.page + 1)
        if page:
            self._set_document_page(page - 1)

    def _set_filter(self, mode: str) -> None:
        self.kind_filter.set(mode)
        self.rescan()

    # ------------------------------------------------------ subfolder depth
    def _depth_changed(self) -> None:
        """Apply a new Tools > Subfolder Depth choice and say what changed."""
        depth = self.scan_depth.get()
        self.app.settings["scan_depth"] = depth
        self._displayed = []          # the row labels change shape, force a rebuild
        self.toast(f"Scanning: {depth_label(depth).lower()}")
        self.rescan()

    def toggle_subfolders(self) -> None:
        """Ctrl+Shift+R: flip between this folder only and the whole tree.

        Toggling back restores the depth that was last chosen from the menu, so
        someone who picked "+ 2 Levels" does not silently get the whole tree.
        """
        current = self.scan_depth.get()
        if current == 0:
            self.scan_depth.set(self._last_depth or D.DEPTH_UNLIMITED)
        else:
            self._last_depth = current
            self.scan_depth.set(0)
        self._depth_changed()

    # ------------------------------------------------- shell integration
    def register_file_types(self) -> None:
        """Register FigView for PDF and open Windows' default-app confirmation.

        Windows does not let a program silently seize a file association: the
        final choice is protected by a per-user hash that only Explorer and the
        Settings app can write. The script therefore does everything that *is*
        allowed - registering the ProgIDs, the "Open with" entries and the app's
        capabilities - and then opens Settings > Default apps for the last click.
        """
        script = self.app.app_dir / "register_file_types.ps1"
        if not script.exists():
            messagebox.showerror("Missing script", f"Not found:\n{script}", parent=self)
            return
        if not messagebox.askokcancel(
                "Set as default PDF reader",
                f"{APP_NAME} will be registered with Windows for PDF documents.\n\n"
                "Nothing outside your own user account is modified, and\n"
                "unregister_file_types.ps1 undoes it.\n\n"
                f"Windows then opens its Default apps page: pick {APP_NAME}\n"
                "there to finish, because only Windows itself may set the final\n"
                "association.", parent=self):
            return
        try:
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", str(script), "-Extensions", ".pdf"]
            # A one-file release extracts scripts into a temporary directory.
            # Register the persistent executable, never that temporary location.
            if getattr(sys, "frozen", False):
                command.extend(["-ExecutablePath", sys.executable])
            subprocess.Popen(
                command,
                cwd=str(self.app.app_dir),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.toast(f"Registering {APP_NAME} for PDF…")
        except Exception as exc:
            messagebox.showerror("Registration failed", str(exc), parent=self)

    def _install_placeholder(self, entry: ttk.Entry, variable: tk.StringVar,
                             text: str) -> None:
        """Draw grey prompt text over an empty entry.

        Tk has no placeholder option. The obvious implementation - insert the
        text and delete it on focus - is wrong here: the entry is backed by a
        StringVar that drives the file filter, so the prompt would be typed
        into the filter and empty the list. This puts a label *on top of* the
        entry instead, so the variable is never written at all; clicking the
        label just forwards focus to the entry underneath.
        """
        p = self.palette
        hint = tk.Label(entry, text=text, background=p.panel_alt, foreground=p.muted,
                        font="FVsmall", borderwidth=0, cursor="xterm")
        hint.bind("<Button-1>", lambda _e: entry.focus_set())

        def refresh(*_args) -> None:
            if variable.get():
                hint.place_forget()
            else:
                hint.place(x=9, rely=0.5, anchor="w")

        variable.trace_add("write", refresh)
        refresh()

    def focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, tk.END)

    def _focus_folder(self) -> None:
        self.folder_entry.focus_set()
        self.folder_entry.select_range(0, tk.END)

    # -------------------------------------------------------------- settings
    def set_pdf_dpi(self) -> None:
        from tkinter import simpledialog

        value = simpledialog.askinteger(
            "PDF render quality",
            "Base rendering DPI for PDF pages.\nHigher = sharper at 100 %, more memory.",
            parent=self, minvalue=50, maxvalue=600, initialvalue=int(self.pdf_dpi.get()))
        if value:
            self.pdf_dpi.set(float(value))
            self.app.settings["pdf_dpi"] = float(value)
            self.app.cache.clear()
            if self.current:
                self.load(self.current, keep_view=False)

    def set_export_dpi(self) -> None:
        from tkinter import simpledialog

        value = simpledialog.askinteger(
            "Copy / export DPI",
            "Resolution used when copying or exporting PDF pages and snips.",
            parent=self, minvalue=72, maxvalue=1200, initialvalue=int(self.export_dpi.get()))
        if value:
            self.export_dpi.set(float(value))
            self.app.settings["export_dpi"] = float(value)
            self.toast(f"Export resolution set to {value} DPI")

    def set_slideshow_interval(self) -> None:
        from tkinter import simpledialog

        value = simpledialog.askfloat("Slideshow interval", "Seconds per figure:",
                                      parent=self, minvalue=0.5, maxvalue=120.0,
                                      initialvalue=self.slideshow_secs.get())
        if value:
            self.slideshow_secs.set(value)
            self.app.settings["slideshow_secs"] = value

    def set_video_frame_step(self) -> None:
        """Configure the frame count used by Up/Down during video playback."""
        from tkinter import simpledialog

        value = simpledialog.askinteger(
            "Video frame jump",
            "Frames jumped by Up / Down during video playback:\n\n"
            "Left / Right always move exactly one frame.",
            parent=self,
            minvalue=VIDEO_FRAME_STEP_MIN,
            maxvalue=VIDEO_FRAME_STEP_MAX,
            initialvalue=self.video_frame_step.get(),
        )
        if value is not None:
            self.video_frame_step.set(value)
            self.app.settings["video_frame_step"] = value
            self.toast(f"Video frame jump set to {value} frames")

    def _slideshow_changed(self) -> None:
        if self._slideshow_job:
            try:
                self.after_cancel(self._slideshow_job)
            except Exception:
                pass
            self._slideshow_job = None
        if self.slideshow_on.get():
            self.toast(f"Slideshow every {self.slideshow_secs.get():g}s - F9 to stop")
            self._slideshow_tick()

    def _slideshow_tick(self) -> None:
        if not self.slideshow_on.get() or self._destroyed:
            return
        self.next_file()
        self._slideshow_job = self.after(int(self.slideshow_secs.get() * 1000), self._slideshow_tick)

    # ------------------------------------------------------------- watching
    def _watch(self) -> None:
        """Poll the folder off-thread; refresh only when something changed."""
        if self._destroyed:
            return
        # Skip while a scan is already in flight: piling requests up would let
        # an older listing overwrite a newer one, and a minimised window has
        # nothing to refresh.
        if self._rescan_pending == 0 and not self._watch_inflight and self.winfo_viewable():
            folder = self.folder
            explicit_paths = (None if self.explicit_paths is None else
                              tuple(self.explicit_paths))
            kinds = D.kinds_for_filter(self.kind_filter.get())
            depth = self.scan_depth.get()
            sort_by = self.sort_by.get()
            descending = self.sort_desc.get()
            self._list_seq += 1
            sequence = self._list_seq
            self._watch_inflight = True

            def job():
                if explicit_paths is not None:
                    return D.entries_for_paths(list(explicit_paths), kinds=kinds), False
                return D.scan_folder(folder, kinds=kinds, max_depth=depth,
                                     sort_by=sort_by, descending=descending)

            def done(result):
                entries, truncated = result
                self._scan_truncated = truncated
                self._watch_inflight = False
                current_paths = (None if self.explicit_paths is None else
                                 tuple(self.explicit_paths))
                if (self._destroyed
                        or (explicit_paths is None and folder != self.folder)
                        or explicit_paths != current_paths
                        or sequence != self._list_seq):
                    return
                self._list_applied = sequence
                if [e.path for e in entries] != [e.path for e in self.entries]:
                    # Files appeared, vanished or were reordered: full refresh.
                    self.entries = entries
                    self._apply_filter()
                    return
                if [e.stamp for e in entries] == [e.stamp for e in self.entries]:
                    return
                # Same files, different bytes: keep the list and the view, and
                # reload only the figure that is actually on screen.
                self.entries = entries
                by_path = {e.path: e for e in entries}
                self.filtered = [by_path.get(e.path, e) for e in self.filtered]
                if self.thumb_size.get():
                    self._queue_thumbnails(self.thumb_size.get())
                if self.current and self.auto_reload.get():
                    fresh = by_path.get(self.current.path)
                    if fresh is not None and fresh.stamp != self.current.stamp:
                        decision = self.edit.reload_decision(fresh)
                        if decision == "adopt":
                            self.edit.adopt_entry(fresh)
                        elif decision == "reload":
                            self.load(fresh, keep_view=True, note="Reloaded")

            def failed(_exc):
                self._watch_inflight = False

            self.app.worker.submit(job, done, failed)
        self._watch_job = self.after(WATCH_MS, self._watch)

    def _update_window_title(self) -> None:
        """Reflect the active source without pretending a file list is a folder."""
        if self.explicit_paths is None:
            suffix = str(self.folder)
        elif self.current is not None:
            suffix = self.current.name
        elif self.explicit_paths:
            suffix = Path(self.explicit_paths[-1]).name
        else:
            suffix = "No files open"
        # The dot is the unsaved-edits marker; edit may not exist during the
        # very first title update in __init__.
        if getattr(self, "edit", None) is not None and self.edit.dirty:
            suffix += "  ●"
        self.title(f"{APP_NAME} - {suffix}")

    # ------------------------------------------------------------ read-outs
    def _update_readouts(self) -> None:
        if self._video_session is not None:
            snapshot = self._video_session.snapshot()
            if snapshot.width and snapshot.height:
                self.page_var.set(
                    f"{snapshot.width} x {snapshot.height} px   {snapshot.fps:.2f} fps"
                )
            else:
                self.page_var.set("Loading video…")
            self.zoom_var.set(snapshot.state.upper())
            return
        display_scale = self.scale
        if self._is_pdf():
            base_dpi = float(getattr(self.document, "base_dpi", self.pdf_dpi.get()))
            display_scale *= base_dpi / PDF_SCREEN_DPI
        self.zoom_var.set(f"{display_scale * 100:.0f}%")
        # Pixel size and paper format live in the Details panel; the page
        # bar already names the PDF page. Only multi-frame images, which
        # have no page bar, announce their frame here.
        if (self.document is None or self.document.page_count < 2
                or self.pdf_controls.winfo_ismapped()):
            self.page_var.set("")
            return
        self.page_var.set(f"frame {self.page + 1} / {self.document.page_count}")

    def _update_status(self, note: str) -> None:
        """Name and position in the list; everything else has its own home
        (Details panel, page bar, zoom readout). Only notes that carry news
        ("Edits discarded", a watcher reload) are shown, transient ones
        such as "Loaded" or "Page" are not."""
        if not self.current:
            return
        index = self._index() + 1
        text = f"{self.current.relname}  ·  {index} of {len(self.filtered)}"
        if note and note not in ("Loaded", "Page", "Ready"):
            text = f"{note}  ·  {text}"
        self.status_var.set(text)

    def _update_info(self) -> None:
        """Refresh the details panel contents."""
        if not self.show_info.get():
            return
        text = self.info_text
        text.configure(state="normal")
        text.delete("1.0", tk.END)
        if self.current:
            rows: list[tuple[str, str]] = [
                ("File", self.current.name),
            ]
            if self.current.depth:
                rows.append(("Subfolder", self.current.subfolder))
            rows += [
                ("Folder", str(self.current.path.parent)),
                ("Size", D.format_size(self.current.size)),
                ("Modified", D.format_time(self.current.mtime)),
            ]
            if self.document is not None:
                rows.extend(self.document.info.items())
                ocr_words = self.text.ocr_word_count()
                if ocr_words is not None:
                    rows.append(("OCR text", f"{ocr_words} words recognised"))
                links = self.links.count()
                if links:
                    rows.append(("Links", f"{links} on this page  ·  click to open"))
            elif self._video_session is not None:
                snapshot = self._video_session.snapshot()
                rows += [
                    ("Media", "Video"),
                    ("State", snapshot.state.capitalize()),
                    ("Dimensions", f"{snapshot.width} x {snapshot.height} px"),
                    ("Frame rate", f"{snapshot.fps:.3g} fps"),
                    ("Duration", V.format_clock(snapshot.duration)),
                    ("Codec", snapshot.codec or "Unknown"),
                    ("Frames", f"{snapshot.frame_count:,}"),
                    ("Audio", "Not available in the OpenCV backend"),
                ]
                if snapshot.error:
                    rows.append(("Playback error", snapshot.error))
            for key, value in rows:
                text.insert(tk.END, f"{key}\n", "key")
                text.insert(tk.END, f"{value}\n\n", "value")
        text.configure(state="disabled")

    def toast(self, message: str) -> None:
        """Transient message in the status bar."""
        if not message:
            return
        self.status_var.set(message)
        if self._toast_job:
            try:
                self.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.after(TOAST_MS, lambda: self._update_status("Ready"))

    # -------------------------------------------------------- context menus
    def _canvas_context_menu(self, event) -> None:
        menu = tk.Menu(self)
        style_menu(menu, self.palette)
        if self._video_session is not None:
            snapshot = self._video_session.snapshot()
            menu.add_command(label="Play" if snapshot.paused else "Pause",
                             command=self.toggle_video)
            menu.add_command(label="Previous Frame", command=lambda: self.step_video_frames(-1))
            menu.add_command(label="Next Frame", command=lambda: self.step_video_frames(1))
            jump = self.video_frame_step.get()
            menu.add_command(label=f"Back {jump} Frames",
                             command=lambda: self.step_video_frames(-jump))
            menu.add_command(label=f"Forward {jump} Frames",
                             command=lambda: self.step_video_frames(jump))
            menu.add_separator()
            menu.add_command(label="Copy Current Frame", command=self.copy_image)
            menu.add_command(label="Save Current Frame As…", command=self.save_page_as)
            menu.add_separator()
            menu.add_command(label="Open in Default App", command=self.open_default_app)
            menu.tk_popup(event.x_root, event.y_root)
            return
        menu.add_command(label="Copy Image", command=self.copy_image)
        menu.add_command(label="Copy Visible View", command=self.copy_view)
        menu.add_command(label="Snip Region", command=lambda: (self.snip_mode.set(True), self._snip_mode_changed()))
        menu.add_separator()
        menu.add_command(label="Fit Page", command=self.fit_page)
        menu.add_command(label="Actual Size", command=self.actual_size)
        menu.add_command(label="Rotate Right", command=lambda: self.rotate(90))
        menu.add_separator()
        menu.add_command(label="Save Page As…", command=self.save_page_as)
        menu.add_command(label="Open in Default App", command=self.open_default_app)
        self.links.context_items(menu, event)
        self.text.context_items(menu, event)
        if self.edit.available():
            menu.add_separator()
            if self.text.has_selection():
                menu.add_command(label="Highlight selected text",
                                 command=self.edit.review.action)
                menu.add_command(label="Comment on selected text",
                                 command=lambda: self.edit.review.action("comment"))
            menu.add_command(label="Review annotations",
                             command=self.edit.review.open_panel)
        self.edit.context_items(menu, event)
        menu.tk_popup(event.x_root, event.y_root)

    def _file_context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and not row.startswith(GROUP_PREFIX):
            self.tree.selection_set(row)
            self._on_tree_select()
        menu = tk.Menu(self)
        style_menu(menu, self.palette)
        menu.add_command(label="Open in Default App", command=self.open_default_app)
        menu.add_command(label="Open in New Window",
                         command=lambda: self.app.new_window(
                             self.current.path.parent if self.current else self.folder,
                             self.current.path if self.current else None,
                             explicit_paths=[self.current.path] if self.current else []))
        menu.add_command(label="Reveal in Explorer", command=self.reveal_in_explorer)
        menu.add_separator()
        menu.add_command(label="Copy Image / Video Frame", command=self.copy_image)
        menu.add_command(label="Copy Path", command=self.copy_path)
        menu.add_command(label="Copy File", command=self.copy_file_object)
        menu.add_separator()
        menu.add_command(label="Delete…", command=self.delete_current)
        menu.tk_popup(event.x_root, event.y_root)

    # ----------------------------------------------------------------- help
    def show_shortcuts(self) -> None:
        rows = [
            ("Navigation", ""),
            ("Up / Down", "Previous / next file; configurable frame jump in video"),
            ("Left / Right", "Previous / next file; one frame in video"),
            ("Home / End", "First / last file"),
            ("PageUp / PageDown", "Previous / next page (multi-page files)"),
            ("Ctrl+G", "Go to page"),
            ("Backspace", "Parent folder"),
            ("Enter", "Open in the default application"),
            ("", ""),
            ("View", ""),
            ("Mouse wheel", "Scroll PDF content/pages; zoom other formats"),
            ("Ctrl + wheel", "Zoom towards the pointer"),
            ("Shift + wheel", "Pan horizontally"),
            ("Drag", "Pan   (middle-drag also works)"),
            ("Double click", "Toggle fit page / 100 %"),
            ("Ctrl+0 / Ctrl+9", "Fit page / fit width"),
            ("Ctrl+Shift+9", "Optimal zoom: fill the width (also the default on open)"),
            ("Ctrl+Shift+0", "Actual size"),
            ("Ctrl+[ / Ctrl+]", "Rotate left / right"),
            ("I", "Invert colours"),
            ("Ctrl+B / Ctrl+I", "Toggle file list / details panel"),
            ("Hide in 5s", "Automatically hide the file list after five seconds"),
            ("Ctrl+T", "Toggle dark and light theme"),
            ("F11", "Full screen"),
            ("", ""),
            ("Text", ""),
            ("Drag over text", "Select text (PDF pages, or an image after OCR)"),
            ("Ctrl + drag", "Select a rectangular block of words"),
            ("Shift + click", "Extend the selection to the click"),
            ("Double / triple click", "Select a word / a whole line"),
            ("Ctrl+A", "Select all text on the page"),
            ("Esc", "Clear the selection"),
            ("Tools > Recognise Text", "OCR a scan or an image; images gain a text layer"),
            ("Ctrl+Shift+T", "OCR the image or scanned page and copy its text"),
            ("Click a link", "Open a web address, e-mail or DOI in the browser; page links jump"),
            ("Ctrl+Shift+I", "Index panel: the PDF's bookmarks, click to jump"),
            ("", ""),
            ("Copy and snip", ""),
            ("S", "Snip mode: drag a rectangle to copy it"),
            ("Shift while dragging", "Constrain the snip to a square"),
            ("Ctrl+C", "Copy the selected text, else the whole page"),
            ("Ctrl+Shift+V", "Copy exactly what is on screen"),
            ("Ctrl+Shift+C", "Copy the last snip again"),
            ("Ctrl+Shift+P", "Copy the file path"),
            ("Ctrl+S / Ctrl+Shift+S", "Save page or video frame / save snip"),
            ("", ""),
            ("PDF", ""),
            ("Page bar", "Previous / next page and detected paper format"),
            ("Wheel", "Scroll the page, then continue into the next page"),
            ("", ""),
            ("Video", ""),
            ("Space / double click", "Play or pause"),
            ("Left / Right", "Previous / next frame"),
            ("Up / Down", f"Jump back / forward {self.video_frame_step.get()} frames"),
            ("Transport bar", "Seek, stop, and toggle looping"),
            ("", ""),
            ("Editing (PDF)", ""),
            ("E", "Enter or leave edit mode"),
            ("V / H / U / K / Q", "Select · highlight · underline · strike · squiggly"),
            ("T / N / P", "Text box · sticky note · freehand ink"),
            ("R / O / L / A / X", "Rectangle · ellipse · line · arrow · redact"),
            ("Ctrl+Z / Ctrl+Y", "Undo / redo any edit"),
            ("Ctrl+S", "Save edits into the PDF (atomic)"),
            ("Ctrl+H", "Find and replace, regex included"),
            ("Ctrl+Shift+E", "Export to Word, Excel, PowerPoint, and more"),
            ("Delete", "Remove the selected annotation"),
            ("Double click", "Edit a text box, or rewrite a paragraph in place"),
            ("", ""),
            ("Files", ""),
            ("Ctrl+N / Ctrl+Shift+N", "New window / duplicate this window"),
            ("Ctrl+O / Ctrl+Shift+O", "Open folder / open file"),
            ("Ctrl+1 / 2 / 3 / 4", "Show images / PDFs / videos / everything"),
            ("Ctrl+Shift+R", "Include subfolders / this folder only"),
            ("Ctrl+F", "Filter the file list"),
            ("F5", "Rescan and reload"),
            ("F9", "Slideshow"),
            ("Delete", "Move the file to the Recycle Bin"),
        ]
        window = tk.Toplevel(self)
        window.title("Keyboard shortcuts")
        window.configure(background=self.palette.bg)
        window.transient(self)
        frame = ttk.Frame(window, padding=(20, 16))
        frame.pack(fill="both", expand=True)
        for key, description in rows:
            if not key and not description:
                ttk.Frame(frame, height=8, style="TFrame").pack(fill="x")
                continue
            if not description:
                ttk.Label(frame, text=key.upper(), style="Accent.TLabel",
                          background=self.palette.bg).pack(anchor="w", pady=(6, 2))
                continue
            row = ttk.Frame(frame)
            row.pack(fill="x")
            ttk.Label(row, text=key, width=24, font="FVmono",
                      foreground=self.palette.muted).pack(side="left")
            ttk.Label(row, text=description).pack(side="left")
        ttk.Button(frame, text="Close", command=window.destroy,
                   style="Accent.TButton").pack(anchor="e", pady=(14, 0))
        window.bind("<Escape>", lambda _e: window.destroy())

    def show_about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            f"A fast document and folder viewer for images, videos, and PDFs.\n\n"
            f"Python {sys.version.split()[0]}\n"
            f"Pillow {Image.__version__}\n"
            f"PDF support: {'available' if D.HAVE_FITZ else 'install pymupdf'}\n"
            f"Video support: {'OpenCV / FFmpeg' if V.backend_available() else 'install opencv-python'}\n"
            f"Video audio: not available in the OpenCV backend\n",
            parent=self)

    # ---------------------------------------------------------------- close
    def _cancel_scheduled_jobs(self) -> None:
        """Cancel every window-owned Tk callback before its interpreter dies."""
        names = (
            "_watch_job", "_quality_job", "_toast_job", "_slideshow_job",
            "_resize_job", "_video_job", "_sidebar_hide_job", "_minimap_job",
        )
        for name in names:
            job = getattr(self, name, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, name, None)

    def close(self) -> None:
        """Persist state, cancel timers and hand back to the app."""
        if self._destroyed:
            return
        # Unsaved PDF edits gate the close; cancel keeps the window open.
        if not self.edit.confirm_close():
            return
        self.edit.detach_for_close()
        self._destroyed = True
        for future in self._thumb_jobs:
            future.cancel()
        if self._load_future is not None:
            self._load_future.cancel()
        self._stop_video_session()
        self._cancel_scheduled_jobs()
        try:
            self.app.settings["geometry"] = self.geometry()
            if self.explicit_paths is None:
                self.app.settings["last_folder"] = str(self.folder)
        except Exception:
            pass
        self.app.forget_window(self)
        try:
            self.destroy()
        except Exception:
            pass
