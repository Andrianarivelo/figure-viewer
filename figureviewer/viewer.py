"""The Figure Viewer window.

One :class:`ViewerWindow` owns one folder. Several windows can be open at the
same time (File > New Window) and they share the worker pool, the document
cache and the settings file through the parent :class:`~figureviewer.app.App`.

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
from . import winclip
from .theme import (DENSITIES, MenuBar, Palette, bind_tooltip, configure_list_density,
                    style_menu, toolbar_button)

ZOOM_MIN = 0.02
ZOOM_MAX = 32.0
ZOOM_STEP = 1.25
QUALITY_DELAY_MS = 110      # idle time before the high-quality repaint
WATCH_MS = 1200             # folder polling interval
TOAST_MS = 2400
MINIMAP_MAX = 190

#: iid prefix for the sub-folder nodes of a grouped listing. A NUL byte cannot
#: appear in a Windows path, so these can never be mistaken for a file row.
GROUP_PREFIX = "\x00dir:"

FIT_PAGE = "page"
FIT_WIDTH = "width"
FIT_FREE = "free"

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
    def __init__(self, app, folder: Path, select: Optional[Path] = None) -> None:
        super().__init__(app.root)
        self.app = app
        self.palette: Palette = app.palette
        self.folder = folder
        self.pending_select = select

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
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._minimap_photo: Optional[ImageTk.PhotoImage] = None
        self._minimap_key: Optional[tuple] = None
        self._minimap_rect: Optional[tuple[float, float, float, float]] = None
        self._thumb_photos: dict[str, ImageTk.PhotoImage] = {}
        self._thumb_keys: dict[str, tuple] = {}
        self._thumb_cache: dict[tuple, Image.Image] = app.thumb_cache
        self._thumb_generation = 0
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
        self._last_depth = int(app.settings.get("scan_depth", 0)) or D.DEPTH_UNLIMITED

        settings = app.settings
        self.search_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=str(folder))
        self.status_var = tk.StringVar(value="Ready")
        self.zoom_var = tk.StringVar(value="100%")
        self.page_var = tk.StringVar(value="")
        self.probe_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="")

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
        self.show_info = tk.BooleanVar(value=bool(settings.get("show_info", False)))
        self.show_minimap = tk.BooleanVar(value=bool(settings.get("show_minimap", True)))
        self.smooth = tk.BooleanVar(value=bool(settings.get("smooth", True)))
        self.invert = tk.BooleanVar(value=False)
        self.grayscale = tk.BooleanVar(value=False)
        self.transparency = tk.StringVar(value=settings.get("transparency", "white"))
        self.thumb_size = tk.IntVar(value=int(settings.get("thumb_size", 24)))
        self.density = tk.StringVar(value=settings.get("density", "compact"))
        self.pdf_dpi = tk.DoubleVar(value=float(settings.get("pdf_dpi", 150.0)))
        self.export_dpi = tk.DoubleVar(value=float(settings.get("export_dpi", 300.0)))
        self.slideshow_secs = tk.DoubleVar(value=float(settings.get("slideshow_secs", 4.0)))
        self.slideshow_on = tk.BooleanVar(value=False)
        self.snip_mode = tk.BooleanVar(value=False)

        self.title(f"{APP_NAME} - {folder}")
        self._apply_icon()
        self.geometry(app.next_geometry())
        self.minsize(880, 560)
        self.configure(background=self.palette.bg)

        self._build_ui()
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
        ttk.Frame(self, style="Rule.TFrame", height=1).grid(row=1, column=0, sticky="ew")

        # --- toolbar --------------------------------------------------------
        bar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 7))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)
        col = 0

        nav = ttk.Frame(bar, style="Toolbar.TFrame")
        nav.grid(row=0, column=col, padx=(0, 10)); col += 1
        toolbar_button(nav, "◀", self.previous_file, p, "Previous file  (Left)", width=3).pack(side="left")
        toolbar_button(nav, "▶", self.next_file, p, "Next file  (Right)", width=3).pack(side="left", padx=(2, 0))
        toolbar_button(nav, "⟳", self.reload_all, p, "Rescan folder and reload  (F5)", width=3).pack(side="left", padx=(6, 0))

        toolbar_button(bar, "Folder…", self.choose_folder, p,
                       "Choose another folder  (Ctrl+O)").grid(row=0, column=col, padx=(0, 6)); col += 1
        toolbar_button(bar, "↑", self.go_parent, p, "Parent folder  (Backspace)", width=3).grid(
            row=0, column=col, padx=(0, 8)); col += 1

        entry = ttk.Entry(bar, textvariable=self.folder_var)
        entry.grid(row=0, column=col, sticky="ew", padx=(0, 8)); col += 1
        entry.bind("<Return>", lambda _e: self.open_typed_folder())
        self.folder_entry = entry

        segment = ttk.Frame(bar, style="Card.TFrame", padding=1)
        segment.grid(row=0, column=col, padx=(0, 10)); col += 1
        for label, value, tip in (("Images", D.FILTER_IMAGES, "Show images only  (Ctrl+1)"),
                                  ("PDF", D.FILTER_DOCS, "Show PDF and vector documents  (Ctrl+2)"),
                                  ("All", D.FILTER_ALL, "Show everything  (Ctrl+3)")):
            radio = ttk.Radiobutton(segment, text=label, value=value, variable=self.kind_filter,
                                    style="Segment.TRadiobutton", takefocus=False,
                                    command=self.rescan)
            radio.pack(side="left")
            bind_tooltip(radio, tip, p)

        zoom = ttk.Frame(bar, style="Toolbar.TFrame")
        zoom.grid(row=0, column=col, padx=(0, 10)); col += 1
        toolbar_button(zoom, "−", lambda: self.zoom_by(1 / ZOOM_STEP), p, "Zoom out  (Ctrl+-)", width=3).pack(side="left")
        toolbar_button(zoom, "+", lambda: self.zoom_by(ZOOM_STEP), p, "Zoom in  (Ctrl++)", width=3).pack(side="left", padx=(2, 0))
        toolbar_button(zoom, "Fit", self.fit_page, p, "Fit the whole page  (Ctrl+0)").pack(side="left", padx=(6, 0))
        toolbar_button(zoom, "1:1", self.actual_size, p,
                       "Actual size, 100 %  (Ctrl+Shift+0)").pack(side="left", padx=(2, 0))

        rot = ttk.Frame(bar, style="Toolbar.TFrame")
        rot.grid(row=0, column=col, padx=(0, 10)); col += 1
        toolbar_button(rot, "↺", lambda: self.rotate(-90), p, "Rotate left  (Ctrl+[)", width=3).pack(side="left")
        toolbar_button(rot, "↻", lambda: self.rotate(90), p, "Rotate right  (Ctrl+])", width=3).pack(side="left", padx=(2, 0))

        self.snip_button = ttk.Checkbutton(
            bar, text="✂ Snip", variable=self.snip_mode, style="Toggle.TCheckbutton",
            takefocus=False, command=self._snip_mode_changed)
        self.snip_button.grid(row=0, column=col, padx=(0, 8)); col += 1
        bind_tooltip(self.snip_button, "Drag a rectangle to copy that region  (S)", p)

        auto = ttk.Checkbutton(bar, text="Auto ⟳", variable=self.auto_reload,
                               style="Toggle.TCheckbutton", takefocus=False)
        auto.grid(row=0, column=col, padx=(0, 0)); col += 1
        bind_tooltip(auto, "Reload the image automatically when the file changes on disk", p)

        # --- body: sidebar | canvas | info ---------------------------------
        body = ttk.Frame(self, style="TFrame")
        body.grid(row=3, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.body = body

        self.sidebar = self._build_sidebar(body)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        stage = ttk.Frame(body, style="TFrame")
        stage.grid(row=0, column=1, sticky="nsew")
        stage.rowconfigure(0, weight=1)
        stage.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(stage, background=p.canvas, highlightthickness=0,
                                borderwidth=0, takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.info_panel = self._build_info_panel(body)

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
        ttk.Label(status, textvariable=self.probe_var, style="Mono.TLabel").grid(row=0, column=2, padx=(0, 14))
        ttk.Label(status, textvariable=self.page_var, style="Status.TLabel").grid(row=0, column=3, padx=(0, 14))
        ttk.Label(status, textvariable=self.zoom_var, style="Mono.TLabel").grid(row=0, column=4)

        self._bind_canvas()
        self._apply_layout()

    def _build_sidebar(self, master: tk.Misc) -> ttk.Frame:
        """File list with search, thumbnails and a count footer."""
        p = self.palette
        frame = ttk.Frame(master, style="Panel.TFrame", padding=(10, 10, 8, 8))
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        header = ttk.Frame(frame, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="FILES", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.count_var, style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        search = ttk.Entry(frame, textvariable=self.search_var)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.search_entry = search
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        bind_tooltip(search, "Filter by name. Space-separated words are ANDed; "
                             "* and ? wildcards work too.  (Ctrl+F)", p)

        holder = ttk.Frame(frame, style="Panel.TFrame")
        holder.grid(row=2, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(holder, columns=("meta",), show="tree", selectmode="browse",
                                 takefocus=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.column("#0", width=250, stretch=True, anchor="w")
        self.tree.column("meta", width=0, stretch=False)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview,
                               style="Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda _e: self.open_default_app())
        self.tree.bind("<Button-3>", self._file_context_menu)
        self.tree.tag_configure("doc", foreground=self.palette.accent)
        self.tree.tag_configure("group", foreground=self.palette.muted)

        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for index in range(3):
            actions.columnconfigure(index, weight=1)
        toolbar_button(actions, "Copy", self.copy_image, p,
                       "Copy the full-resolution image  (Ctrl+C)").grid(row=0, column=0, sticky="ew", padx=(0, 3))
        toolbar_button(actions, "Path", self.copy_path, p,
                       "Copy the file path  (Ctrl+Shift+P)").grid(row=0, column=1, sticky="ew", padx=3)
        toolbar_button(actions, "Open", self.open_default_app, p,
                       "Open in the default application  (Enter)").grid(row=0, column=2, sticky="ew", padx=(3, 0))
        return frame

    def _build_info_panel(self, master: tk.Misc) -> ttk.Frame:
        """Right-hand metadata panel (hidden by default)."""
        p = self.palette
        frame = ttk.Frame(master, style="Panel.TFrame", padding=(12, 10))
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="DETAILS", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        text = tk.Text(frame, width=32, wrap="word", background=p.panel, foreground=p.text,
                       insertbackground=p.text, relief="flat", highlightthickness=0,
                       font="FVsmall", padx=0, pady=8, spacing1=1, spacing3=3, cursor="arrow")
        text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
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
                              command=lambda: self.app.new_window(self.folder, self.current.path if self.current else None))
        file_menu.add_separator()
        file_menu.add_command(label="Open Folder…", accelerator="Ctrl+O", command=self.choose_folder)
        file_menu.add_command(label="Open File…", accelerator="Ctrl+Shift+O", command=self.choose_file)
        self.recent_menu = tk.Menu(file_menu)
        style_menu(self.recent_menu, p)
        file_menu.add_cascade(label="Recent Folders", menu=self.recent_menu)
        file_menu.add_command(label="Parent Folder", accelerator="Backspace", command=self.go_parent)
        file_menu.add_separator()
        file_menu.add_command(label="Save Page As…", accelerator="Ctrl+S", command=self.save_page_as)
        file_menu.add_command(label="Save Snip As…", accelerator="Ctrl+Shift+S", command=self.save_snip_as)
        file_menu.add_command(label="Export All Pages…", command=self.export_all_pages)
        file_menu.add_separator()
        file_menu.add_command(label="Open in Default App", accelerator="Enter", command=self.open_default_app)
        file_menu.add_command(label="Reveal in Explorer", accelerator="Ctrl+E", command=self.reveal_in_explorer)
        file_menu.add_separator()
        file_menu.add_command(label="Close Window", accelerator="Ctrl+W", command=self.close)
        file_menu.add_command(label="Exit", accelerator="Alt+F4", command=self.app.quit_all)

        edit_menu = self.menubar.add_menu("Edit", underline=0)
        edit_menu.add_command(label="Copy Image", accelerator="Ctrl+C", command=self.copy_image)
        edit_menu.add_command(label="Copy Visible View", accelerator="Ctrl+Shift+V", command=self.copy_view)
        edit_menu.add_command(label="Copy Last Snip", accelerator="Ctrl+Shift+C", command=self.copy_last_snip)
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy File Path", accelerator="Ctrl+Shift+P", command=self.copy_path)
        edit_menu.add_command(label="Copy File (paste into Explorer)", command=self.copy_file_object)
        edit_menu.add_command(label="Copy Folder Path", command=self.copy_folder_path)
        edit_menu.add_command(label="Copy Page Text (PDF)", command=self.copy_page_text)
        edit_menu.add_separator()
        edit_menu.add_command(label="Snip Region", accelerator="S", command=lambda: self.snip_mode.set(True) or self._snip_mode_changed())
        edit_menu.add_command(label="Find in File List", accelerator="Ctrl+F", command=self.focus_search)

        view_menu = self.menubar.add_menu("View", underline=0)
        view_menu.add_command(label="Fit Page", accelerator="Ctrl+0", command=self.fit_page)
        view_menu.add_command(label="Fit Width", accelerator="Ctrl+9", command=self.fit_width)
        view_menu.add_command(label="Actual Size", accelerator="Ctrl+Shift+0", command=self.actual_size)
        view_menu.add_command(label="Zoom In", accelerator="Ctrl++", command=lambda: self.zoom_by(ZOOM_STEP))
        view_menu.add_command(label="Zoom Out", accelerator="Ctrl+-", command=lambda: self.zoom_by(1 / ZOOM_STEP))
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
                                  command=self._apply_layout)
        view_menu.add_checkbutton(label="Details Panel", accelerator="Ctrl+I", variable=self.show_info,
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
        go_menu.add_command(label="Next File", accelerator="Right", command=self.next_file)
        go_menu.add_command(label="Previous File", accelerator="Left", command=self.previous_file)
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
        tools_menu.add_separator()
        tools_menu.add_checkbutton(label="Slideshow", accelerator="F9", variable=self.slideshow_on,
                                   command=self._slideshow_changed)
        tools_menu.add_command(label="Slideshow Interval…", command=self.set_slideshow_interval)
        tools_menu.add_separator()
        tools_menu.add_command(label="PDF Render Quality…", command=self.set_pdf_dpi)
        tools_menu.add_command(label="Copy / Export DPI…", command=self.set_export_dpi)
        tools_menu.add_separator()
        tools_menu.add_command(label="Rescan Folder", accelerator="F5", command=self.reload_all)
        tools_menu.add_command(label="Clear Caches", command=self.clear_caches)
        tools_menu.add_separator()
        tools_menu.add_command(label="Set as Default Image / PDF Viewer…",
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
        """Rebuild the Recent Folders cascade from the shared settings."""
        self.recent_menu.delete(0, "end")
        recents = self.app.settings.get("recent", [])
        if not recents:
            self.recent_menu.add_command(label="(empty)", state="disabled")
            return
        for path in recents[:12]:
            self.recent_menu.add_command(
                label=path, command=lambda p=path: self.set_folder(Path(p)))
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear List", command=self._clear_recent)

    def _clear_recent(self) -> None:
        self.app.settings["recent"] = []
        self.app.broadcast_recent()

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
        bind("<Control-N>", lambda e: self.app.new_window(self.folder,
                                                          self.current.path if self.current else None))
        bind("<Control-o>", lambda e: self.choose_folder())
        bind("<Control-O>", lambda e: self.choose_file())
        bind("<Control-w>", lambda e: self.close())
        bind("<Control-s>", lambda e: self.save_page_as())
        bind("<Control-S>", lambda e: self.save_snip_as())
        bind("<Control-e>", lambda e: self.reveal_in_explorer())
        bind("<Control-f>", lambda e: self.focus_search())
        bind("<Control-g>", lambda e: self.goto_page())
        bind("<Control-b>", lambda e: (self.show_sidebar.set(not self.show_sidebar.get()), self._apply_layout()))
        bind("<Control-i>", lambda e: (self.show_info.set(not self.show_info.get()), self._apply_layout()))
        bind("<Control-t>", lambda e: self.app.toggle_theme())
        bind("<Control-c>", self._copy_shortcut)
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
        bind("<Control-parenright>", lambda e: self.actual_size())
        bind("<Control-bracketleft>", lambda e: self.rotate(-90))
        bind("<Control-bracketright>", lambda e: self.rotate(90))
        bind("<Control-Key-1>", lambda e: self._set_filter(D.FILTER_IMAGES))
        bind("<Control-Key-2>", lambda e: self._set_filter(D.FILTER_DOCS))
        bind("<Control-Key-3>", lambda e: self._set_filter(D.FILTER_ALL))

        bind("<Left>", guard(self.previous_file))
        bind("<Right>", guard(self.next_file))
        bind("<Up>", guard(self.previous_file))
        bind("<Down>", guard(self.next_file))
        bind("<Home>", guard(lambda: self.select_index(0)))
        bind("<End>", guard(lambda: self.select_index(len(self.filtered) - 1)))
        bind("<Prior>", guard(lambda: self.change_page(-1)))
        bind("<Next>", guard(lambda: self.change_page(1)))
        bind("<BackSpace>", guard(self.go_parent))
        bind("<Return>", guard(self.open_default_app))
        bind("<F1>", lambda e: self.show_shortcuts())
        bind("<F5>", lambda e: self.reload_all())
        bind("<F9>", lambda e: (self.slideshow_on.set(not self.slideshow_on.get()), self._slideshow_changed()))
        bind("<F11>", lambda e: self.toggle_fullscreen())
        bind("<Escape>", self._on_escape)
        bind("<s>", guard(lambda: (self.snip_mode.set(not self.snip_mode.get()), self._snip_mode_changed())))
        bind("<i>", guard(lambda: (self.invert.set(not self.invert.get()), self.redraw_now())))
        bind("<Delete>", guard(self.delete_current))

    def _bind_canvas(self) -> None:
        canvas = self.canvas
        canvas.bind("<Configure>", self._on_canvas_configure)
        canvas.bind("<MouseWheel>", self._on_wheel)
        canvas.bind("<Shift-MouseWheel>", lambda e: self._pan_by(-e.delta * 0.6, 0))
        canvas.bind("<Control-MouseWheel>", lambda e: self._pan_by(0, -e.delta * 0.6))
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<ButtonPress-2>", self._on_middle_press)
        canvas.bind("<B2-Motion>", self._on_middle_drag)
        canvas.bind("<ButtonRelease-2>", lambda e: self._end_pan())
        canvas.bind("<Double-1>", self._on_double_click)
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
        if self.show_sidebar.get():
            self.sidebar.grid(row=0, column=0, sticky="nsew")
            self.body.columnconfigure(0, weight=0, minsize=210)
        else:
            self.sidebar.grid_remove()
            self.body.columnconfigure(0, minsize=0)
        if self.show_info.get():
            self.info_panel.grid(row=0, column=2, sticky="nsew")
            self._update_info()
        else:
            self.info_panel.grid_remove()
        self.app.settings["show_sidebar"] = self.show_sidebar.get()
        self.app.settings["show_info"] = self.show_info.get()

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
        patterns = " ".join(f"*{s}" for s in sorted(D.RASTER_SUFFIXES | D.VECTOR_SUFFIXES))
        chosen = filedialog.askopenfilename(
            parent=self, initialdir=str(self.folder), title="Open a file",
            filetypes=[("Supported files", patterns), ("All files", "*.*")])
        if chosen:
            path = Path(chosen)
            self.set_folder(path.parent, select=path)

    def open_typed_folder(self) -> None:
        raw = self.folder_var.get().strip().strip('"')
        if not raw:
            return
        path = Path(raw).expanduser()
        if path.is_file():
            self.set_folder(path.parent, select=path)
        else:
            self.set_folder(path)

    def reveal_path(self, target: Path) -> None:
        """Show ``target`` in this window, reusing the current listing if it can.

        Called when a second launch hands a file over (see
        :class:`figureviewer.app.InstanceServer`). Staying in the current folder
        matters: if the user is browsing ``exp/`` three levels deep and opens
        ``exp/run7/figures/psth.png`` from Explorer, jumping the window to
        ``figures/`` would throw away the listing they were working in.
        """
        try:
            target = target.expanduser().resolve()
        except Exception:
            pass

        if target.is_dir():
            if target != self.folder:
                self.set_folder(target)
            return

        # A PDF opened while the toolbar says "Images" would land in an empty
        # list; widen the filter rather than showing nothing.
        kind = D.suffix_kind(target.suffix)
        if kind is not None and kind not in D.kinds_for_filter(self.kind_filter.get()):
            self.kind_filter.set(D.FILTER_ALL)

        if self._covers(target):
            self.pending_select = target
            self.rescan(keep_selection=False)
        else:
            self.set_folder(target.parent, target)

    def _covers(self, target: Path) -> bool:
        """True if the current folder and scan depth already list ``target``."""
        try:
            relative = target.relative_to(self.folder)
        except ValueError:
            return False
        depth = self.scan_depth.get()
        levels = len(relative.parts) - 1
        return depth < 0 or levels <= depth

    def go_parent(self) -> None:
        parent = self.folder.parent
        if parent != self.folder:
            self.set_folder(parent)

    def set_folder(self, folder: Path, select: Optional[Path] = None) -> None:
        """Switch to another folder, remembering it in the recent list."""
        try:
            folder = folder.expanduser().resolve()
        except Exception:
            pass
        if not folder.is_dir():
            messagebox.showerror("Folder not found", str(folder), parent=self)
            self.folder_var.set(str(self.folder))
            return
        self.folder = folder
        self.folder_var.set(str(folder))
        self.title(f"{APP_NAME} - {folder}")
        self.current = None
        self.document = None
        self.page = 0
        self.pending_select = select
        self.canvas.delete("all")
        self.app.push_recent(folder)
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
        kinds = D.kinds_for_filter(self.kind_filter.get())
        depth = self.scan_depth.get()
        sort_by = self.sort_by.get()
        descending = self.sort_desc.get()
        keep = self.current.path if (keep_selection and self.current) else self.pending_select

        def job():
            return D.scan_folder(folder, kinds=kinds, max_depth=depth,
                                 sort_by=sort_by, descending=descending)

        def done(result):
            entries, truncated = result
            self._rescan_pending = max(0, self._rescan_pending - 1)
            if self._destroyed or folder != self.folder or sequence < self._list_applied:
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
        grouped = (self.scan_depth.get() != 0
                   and self.sort_by.get() in ("name", "kind")
                   and any(entry.depth for entry in self.filtered))

        rows = [str(entry.path) for entry in self.filtered]
        if rows != self._displayed or grouped != self._grouped:
            self._suppress_select = True
            self.tree.delete(*self.tree.get_children())
            groups: dict[str, str] = {"": ""}     # sub-folder -> parent node iid
            for entry in self.filtered:
                parent = self._ensure_group(entry.subfolder, groups) if grouped else ""
                label = entry.name if grouped else entry.relname
                self.tree.insert(parent, "end", iid=str(entry.path), text=f"  {label}",
                                 tags=("doc",) if entry.kind == D.KIND_DOC else ())
            self._suppress_select = False
            self._displayed = rows
            self._grouped = grouped
            self._thumb_photos = {iid: photo for iid, photo in self._thumb_photos.items()
                                  if iid in set(rows)}
            for iid, photo in self._thumb_photos.items():
                try:
                    self.tree.item(iid, image=photo)
                except tk.TclError:
                    pass

        docs_count = sum(1 for e in self.filtered if e.kind == D.KIND_DOC)
        images = len(self.filtered) - docs_count
        count = (f"{images} img · {docs_count} doc" if docs_count
                 else f"{len(self.filtered)}")
        if self.scan_depth.get() != 0:
            folders = len({e.subfolder for e in self.filtered})
            if folders > 1:
                count += f" · {folders} dirs"
        self.count_var.set(count)

        if size:
            self._queue_thumbnails(size)

        if not self.filtered:
            self.current = None
            self.document = None
            self.canvas.delete("all")
            self._draw_empty()
            self.status_var.set(f"No matching files in {self.folder}")
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
            # Same file: only reload when the bytes changed.
            fresh = self.filtered[index]
            if self.auto_reload.get() and self.current and fresh.stamp != self.current.stamp:
                self.load(fresh, keep_view=True, note="Reloaded")
            else:
                self.current = fresh

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
                         open=True, tags=("group",))
        groups[subfolder] = iid
        return iid

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

        for entry in self.filtered[:400]:
            iid = str(entry.path)
            key = entry.key + (size,)
            if self._thumb_keys.get(iid) == key:
                continue                       # already on screen and current
            cached = self._thumb_cache.get(key)
            if cached is not None:
                apply((entry, cached))
                continue
            self.app.worker.submit(lambda e=entry: make(e), apply, lambda _exc: None)

    # ------------------------------------------------------------- loading
    def load(self, entry: D.Entry, *, keep_view: bool = False, note: str = "Loaded") -> None:
        """Load a document on a worker thread and display it."""
        self.current = entry
        token = time.perf_counter()
        self._load_token = token
        dpi = self.pdf_dpi.get()

        def job():
            return self.app.cache.get(entry, pdf_dpi=dpi)

        def done(document):
            if self._destroyed or getattr(self, "_load_token", None) != token:
                return
            self.document = document
            if not keep_view:
                self.page = 0
                self.rotation = 0
                self.flip_h = self.flip_v = False
                self.flip_h_var().set(False)
                self.flip_v_var().set(False)
                self.fit_mode = FIT_PAGE
            self._minimap_key = None
            self._invalidate()
            if not keep_view:
                self.fit_page()
            else:
                self.redraw_now()
            self._update_status(note)
            self._update_info()
            self._prefetch_neighbours()

        def failed(exc):
            if self._destroyed:
                return
            self.document = None
            self.canvas.delete("all")
            self._draw_message(str(exc))
            self.status_var.set(f"Could not open {entry.relname}: {exc}")

        self.app.worker.submit(job, done, failed)

    def _prefetch_neighbours(self) -> None:
        """Warm the cache with the next and previous file so navigation is instant."""
        index = self._index()
        dpi = self.pdf_dpi.get()
        for offset in (1, -1):
            position = index + offset
            if 0 <= position < len(self.filtered):
                entry = self.filtered[position]
                if self.app.cache.peek(entry, pdf_dpi=dpi) is None:
                    self.app.worker.submit(
                        lambda e=entry: self.app.cache.get(e, pdf_dpi=dpi),
                        lambda _r: None, lambda _e: None)

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

    def _fit_scale(self, mode: str) -> float:
        width, height = self.logical_size()
        cw, ch = self._canvas_size()
        margin = 16
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

    def actual_size(self) -> None:
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

    def _draw_empty(self) -> None:
        message = ("Drop a folder path in the bar above, or press Ctrl+O."
                   if self.entries else f"Nothing to show in\n{self.folder}")
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
            if self.document is None:
                self._draw_empty()
                return
            if self.fit_mode in (FIT_PAGE, FIT_WIDTH):
                self.scale = self._fit_scale(self.fit_mode)
            self._invalidate()
            self.redraw_now()

        self._resize_job = self.after(40, apply)

    def _on_wheel(self, event) -> None:
        if self.document is None:
            return
        factor = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        self.zoom_by(factor, focus=(event.x, event.y))

    def _on_press(self, event) -> None:
        self.canvas.focus_set()
        if self._minimap_click(event):
            return
        if self.snip_mode.get():
            self._snip_start = (event.x, event.y)
            self._snip_rect = None
            return
        self._pan_anchor = (event.x, event.y, self.off_x, self.off_y)
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, event) -> None:
        if self._snip_start is not None:
            self._update_snip_overlay(event.x, event.y, shift=bool(event.state & 0x0001))
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
        if self._snip_start is not None:
            self._finish_snip(event.x, event.y)
            return
        self._end_pan()

    def _end_pan(self) -> None:
        self._pan_anchor = None
        self.canvas.configure(cursor="crosshair" if self.snip_mode.get() else "")

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
        if abs(self.scale - 1.0) < 1e-6:
            self.fit_page()
        else:
            self.actual_size()

    def _on_motion(self, event) -> None:
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
            self.swatch.configure(background=colour or self.palette.panel)
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
        if self.attributes("-fullscreen"):
            self.attributes("-fullscreen", False)
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
        self.canvas.configure(cursor="crosshair" if active else "")
        if active:
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
        """Ctrl+C copies the image, unless a text field has focus."""
        if self._typing():
            return None
        self.copy_image()
        return "break"

    def copy_image(self) -> None:
        """Copy the whole current page at full resolution."""
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
        winclip.copy_text(str(self.folder))
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
        if self.document is None:
            return
        text = self.document.text(self.page)
        if not text.strip():
            self.toast("No extractable text on this page")
            return
        winclip.copy_text(text)
        self.toast(f"Copied {len(text)} characters of page text")

    def save_page_as(self) -> None:
        """Write the current page to a new file."""
        if self.document is None or self.current is None:
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
        else:
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
        try:
            self._recycle(path)
        except Exception as exc:
            messagebox.showerror("Could not delete", f"{path}\n\n{exc}", parent=self)
            return
        index = self._index()
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
        self.page = page
        self._minimap_key = None
        if self.fit_mode != FIT_FREE:
            self.scale = self._fit_scale(self.fit_mode)
        self.off_x = self.off_y = 0.0
        self._invalidate()
        self.redraw_now()
        self._update_status("Page")
        self._update_info()

    def goto_page(self) -> None:
        if self.document is None or self.document.page_count < 2:
            return
        from tkinter import simpledialog

        page = simpledialog.askinteger(
            "Go to page", f"Page number (1 - {self.document.page_count}):",
            parent=self, minvalue=1, maxvalue=self.document.page_count, initialvalue=self.page + 1)
        if page:
            self.page = page - 1
            self._minimap_key = None
            if self.fit_mode != FIT_FREE:
                self.scale = self._fit_scale(self.fit_mode)
            self.off_x = self.off_y = 0.0
            self._invalidate()
            self.redraw_now()
            self._update_info()

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
        """Run register_file_types.ps1 so Windows can offer this app as default.

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
                "Set as default viewer",
                "Figure Viewer will be registered with Windows for images and PDFs.\n\n"
                "Nothing outside your own user account is modified, and\n"
                "unregister_file_types.ps1 undoes it.\n\n"
                "Windows then opens its Default apps page: pick Figure Viewer\n"
                "there to finish, because only Windows itself may set the final\n"
                "association.", parent=self):
            return
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                cwd=str(self.app.app_dir),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.toast("Registering file types…")
        except Exception as exc:
            messagebox.showerror("Registration failed", str(exc), parent=self)

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
            kinds = D.kinds_for_filter(self.kind_filter.get())
            depth = self.scan_depth.get()
            sort_by = self.sort_by.get()
            descending = self.sort_desc.get()
            self._list_seq += 1
            sequence = self._list_seq
            self._watch_inflight = True

            def job():
                return D.scan_folder(folder, kinds=kinds, max_depth=depth,
                                     sort_by=sort_by, descending=descending)

            def done(result):
                entries, truncated = result
                self._scan_truncated = truncated
                self._watch_inflight = False
                if self._destroyed or folder != self.folder or sequence < self._list_applied:
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
                        self.load(fresh, keep_view=True, note="Reloaded")

            def failed(_exc):
                self._watch_inflight = False

            self.app.worker.submit(job, done, failed)
        self._watch_job = self.after(WATCH_MS, self._watch)

    # ------------------------------------------------------------ read-outs
    def _update_readouts(self) -> None:
        self.zoom_var.set(f"{self.scale * 100:.0f}%")
        if self.document is None:
            self.page_var.set("")
            return
        width, height = self.document.page_size(self.page)
        parts = [f"{width} x {height} px"]
        if self.document.page_count > 1:
            parts.insert(0, f"page {self.page + 1}/{self.document.page_count}")
        self.page_var.set("   ".join(parts))

    def _update_status(self, note: str) -> None:
        if not self.current:
            return
        index = self._index() + 1
        self.status_var.set(
            f"{note}  ·  {self.current.relname}  ·  {D.format_size(self.current.size)}"
            f"  ·  {D.format_time(self.current.mtime)}  ·  {index}/{len(self.filtered)}")

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
                         command=lambda: self.app.new_window(self.folder,
                                                             self.current.path if self.current else None))
        menu.add_command(label="Reveal in Explorer", command=self.reveal_in_explorer)
        menu.add_separator()
        menu.add_command(label="Copy Image", command=self.copy_image)
        menu.add_command(label="Copy Path", command=self.copy_path)
        menu.add_command(label="Copy File", command=self.copy_file_object)
        menu.add_separator()
        menu.add_command(label="Delete…", command=self.delete_current)
        menu.tk_popup(event.x_root, event.y_root)

    # ----------------------------------------------------------------- help
    def show_shortcuts(self) -> None:
        rows = [
            ("Navigation", ""),
            ("Left / Right / Up / Down", "Previous / next file"),
            ("Home / End", "First / last file"),
            ("PageUp / PageDown", "Previous / next page (multi-page files)"),
            ("Ctrl+G", "Go to page"),
            ("Backspace", "Parent folder"),
            ("Enter", "Open in the default application"),
            ("", ""),
            ("View", ""),
            ("Mouse wheel", "Zoom towards the pointer"),
            ("Shift / Ctrl + wheel", "Pan horizontally / vertically"),
            ("Drag", "Pan   (middle-drag also works)"),
            ("Double click", "Toggle fit page / 100 %"),
            ("Ctrl+0 / Ctrl+9", "Fit page / fit width"),
            ("Ctrl+Shift+0", "Actual size"),
            ("Ctrl+[ / Ctrl+]", "Rotate left / right"),
            ("I", "Invert colours"),
            ("Ctrl+B / Ctrl+I", "Toggle sidebar / details panel"),
            ("Ctrl+T", "Toggle dark and light theme"),
            ("F11", "Full screen"),
            ("", ""),
            ("Copy and snip", ""),
            ("S", "Snip mode: drag a rectangle to copy it"),
            ("Shift while dragging", "Constrain the snip to a square"),
            ("Ctrl+C", "Copy the whole page"),
            ("Ctrl+Shift+V", "Copy exactly what is on screen"),
            ("Ctrl+Shift+C", "Copy the last snip again"),
            ("Ctrl+Shift+P", "Copy the file path"),
            ("Ctrl+S / Ctrl+Shift+S", "Save page / snip as a file"),
            ("", ""),
            ("Files", ""),
            ("Ctrl+N / Ctrl+Shift+N", "New window / duplicate this window"),
            ("Ctrl+O / Ctrl+Shift+O", "Open folder / open file"),
            ("Ctrl+1 / 2 / 3", "Show images / PDFs / everything"),
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
            f"A fast folder-based viewer for figures and PDFs.\n\n"
            f"Python {sys.version.split()[0]}\n"
            f"Pillow {Image.__version__}\n"
            f"PDF support: {'available' if D.HAVE_FITZ else 'install pymupdf'}\n",
            parent=self)

    # ---------------------------------------------------------------- close
    def close(self) -> None:
        """Persist state, cancel timers and hand back to the app."""
        if self._destroyed:
            return
        self._destroyed = True
        for job in (self._watch_job, self._quality_job, self._toast_job,
                    self._slideshow_job, self._resize_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        try:
            self.app.settings["geometry"] = self.geometry()
            self.app.settings["last_folder"] = str(self.folder)
        except Exception:
            pass
        self.app.forget_window(self)
        try:
            self.destroy()
        except Exception:
            pass
