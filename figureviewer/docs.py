"""Document model, folder scanning and caching.

Design goals
------------
1. **Never lock a file.** Every document is loaded from an in-memory copy of
   the bytes, so a plotting script can overwrite a figure while it is on screen.
2. **Never rasterise more than the screen needs.** Both document types can
   render an arbitrary *region* at an arbitrary *scale*, so zooming to 800 % on
   a 12000 x 8000 px figure costs the same as viewing it at 100 %.
3. **Never block the UI thread.** Scanning, decoding and thumbnailing all run
   on a small worker pool; results are delivered through a queue.

Two still-document kinds are implemented behind one interface. Video playback
uses a separate streaming session in :mod:`figureviewer.video`, because loading
an entire movie into the document cache would be both slow and wasteful:

``RasterDocument``  PNG / JPEG / TIFF / BMP / GIF / WebP, including multi-page
                    TIFFs and animated GIFs (each frame is a page).
``VectorDocument``  PDF (and, when PyMuPDF is present, SVG / EPS / XPS / CBZ).
                    Pages are re-rasterised at the exact zoom level, so text
                    and lines stay razor sharp at any magnification.
"""
from __future__ import annotations

import os
import importlib
import importlib.util
import queue
import stat as stat_module
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps

from . import VIDEO_SUFFIXES

# Pillow's guard against decompression-bomb attacks trips on legitimate
# high-resolution scientific figures; raise it rather than disable it.
Image.MAX_IMAGE_PIXELS = 512_000_000

def _find_pdf_backend() -> bool:
    """Check install metadata without loading PyMuPDF's native libraries."""
    for name in ("pymupdf", "fitz"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


# Availability initially means installed. If an installed native backend fails
# to load, the first PDF request records the actual error and disables support.
HAVE_FITZ = _find_pdf_backend()
FITZ_ERROR = "" if HAVE_FITZ else "PyMuPDF is not installed"
_fitz_module = None
_fitz_lock = threading.Lock()


def _load_fitz():
    """Import the optional renderer once, only when a PDF operation needs it."""
    global _fitz_module, HAVE_FITZ, FITZ_ERROR
    if _fitz_module is not None:
        return _fitz_module
    with _fitz_lock:
        if _fitz_module is not None:
            return _fitz_module
        if HAVE_FITZ:
            try:
                try:
                    backend = importlib.import_module("pymupdf")
                except ImportError:
                    backend = importlib.import_module("fitz")
                # An unrelated historical package also used the fitz name.
                # Reject it here instead of reporting a cryptic missing method.
                if not callable(getattr(backend, "open", None)):
                    raise ImportError("The installed fitz module is not PyMuPDF")
                _fitz_module = backend
                return backend
            except Exception as exc:
                HAVE_FITZ = False
                FITZ_ERROR = str(exc)
        raise RuntimeError(
            "PDF support requires a working PyMuPDF installation.\n\n"
            "Install or repair it with:\n    pip install --upgrade pymupdf\n\n"
            f"Renderer error: {FITZ_ERROR}"
        )


class _LazyFitz:
    """Preserve the shared ``docs.fitz`` API without eager renderer imports."""

    def __getattr__(self, name):
        return getattr(_load_fitz(), name)


fitz = _LazyFitz()


RASTER_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".gif",
    ".webp", ".ppm", ".pgm", ".jp2", ".ico",
}
#: Vector / paged formats, only usable when PyMuPDF is importable.
VECTOR_SUFFIXES = {".pdf", ".svg", ".eps", ".xps", ".oxps", ".cbz", ".epub"}
# Imported from the already-loaded package root. The playback backend is still
# loaded only when a video window or video thumbnail is actually requested.

KIND_IMAGE = "image"
KIND_DOC = "doc"
KIND_VIDEO = "video"

#: Filter modes offered by the toolbar's segmented switch.
FILTER_IMAGES = "images"
FILTER_DOCS = "docs"
FILTER_VIDEOS = "videos"
FILTER_ALL = "all"


def suffix_kind(suffix: str) -> Optional[str]:
    """Return the media kind for a supported suffix, else ``None``."""
    low = suffix.lower()
    if low in RASTER_SUFFIXES:
        return KIND_IMAGE
    if low in VECTOR_SUFFIXES and HAVE_FITZ:
        return KIND_DOC
    if low == ".pdf":
        return KIND_DOC          # listed even without a renderer, so the user
    if low in VIDEO_SUFFIXES:
        return KIND_VIDEO
    return None                  # gets a helpful message instead of silence


def kinds_for_filter(mode: str) -> set[str]:
    """Map a toolbar filter mode to the set of document kinds to show."""
    if mode == FILTER_IMAGES:
        return {KIND_IMAGE}
    if mode == FILTER_DOCS:
        return {KIND_DOC}
    if mode == FILTER_VIDEOS:
        return {KIND_VIDEO}
    return {KIND_IMAGE, KIND_DOC, KIND_VIDEO}


@dataclass(frozen=True)
class Entry:
    """Immutable metadata for one file on disk.

    ``root`` is the folder the scan started from. It is what lets a recursive
    listing show ``run3/figures/psth.png`` instead of three indistinguishable
    ``psth.png`` rows, and what makes files group by sub-folder when sorted.
    """

    path: Path
    size: int
    mtime: float
    kind: str
    root: Optional[Path] = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def relname(self) -> str:
        """Path relative to the scan root, e.g. ``run3/figures/psth.png``.

        Falls back to the bare file name when the entry has no root, or when
        the file somehow sits outside it (a junction pointing elsewhere).
        """
        if self.root is None:
            return self.path.name
        try:
            return str(self.path.relative_to(self.root))
        except ValueError:
            return self.path.name

    @property
    def subfolder(self) -> str:
        """The directory part of :attr:`relname`; empty for top-level files."""
        return os.path.dirname(self.relname)

    @property
    def depth(self) -> int:
        """0 for a file directly in the scan root, 1 one level down, and so on."""
        head = self.subfolder
        return 0 if not head else head.count(os.sep) + 1

    @property
    def stamp(self) -> tuple[int, float]:
        """Change token: a new value means the bytes on disk changed."""
        return (self.size, self.mtime)

    @property
    def key(self) -> tuple[str, int, float]:
        """Cache key that is invalidated automatically when the file changes."""
        return (str(self.path), self.size, self.mtime)


def natural_key(text: str) -> tuple:
    """Sort key placing ``Figure2`` before ``Figure10``."""
    parts: list = []
    chunk = ""
    is_digit = False
    for ch in text:
        if ch.isdigit() != is_digit and chunk:
            parts.append((1, int(chunk), "") if is_digit else (0, 0, chunk.lower()))
            chunk = ""
        chunk += ch
        is_digit = ch.isdigit()
    if chunk:
        parts.append((1, int(chunk), "") if is_digit else (0, 0, chunk.lower()))
    return tuple(parts)


def folder_key(entry: Entry) -> tuple:
    """Sort key for an entry's sub-folder, one natural key per path segment.

    Top-level files get an empty tuple, so they always sort before the
    sub-folders underneath them, and every sub-folder's files stay contiguous
    instead of being interleaved by file name across the whole tree.
    """
    head = entry.subfolder
    if not head:
        return ()
    return tuple(natural_key(part) for part in head.split(os.sep))


#: Sort modes offered by Tools > Sort By. ``name`` and ``kind`` are
#: folder-aware so that a multi-level scan reads like a directory tree;
#: ``modified`` and ``size`` deliberately ignore the folder, because "the ten
#: newest figures anywhere under here" is exactly why one sorts by date.
SORT_KEYS: dict[str, Callable[[Entry], object]] = {
    "name": lambda e: (folder_key(e), natural_key(e.name)),
    "modified": lambda e: e.mtime,
    "size": lambda e: e.size,
    "kind": lambda e: (e.kind, folder_key(e), natural_key(e.name)),
}


#: Directories never worth walking into during a recursive scan. Skipping them
#: is not cosmetic: ``node_modules`` or a conda env under the folder you point
#: at can hold tens of thousands of icons and turn a scan into a stall.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".ipynb_checkpoints",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules", ".tox",
    "site-packages", "$recycle.bin", "system volume information",
}

#: ``max_depth`` value meaning "walk the whole tree, however deep it goes".
DEPTH_UNLIMITED = -1

#: Hard stop on one scan. A recursive scan of a drive root would otherwise
#: build a list no human can use and eat memory doing it. The caller is told
#: the listing was cut short so it can say so instead of quietly lying.
MAX_SCAN_ENTRIES = 20_000

# Canonical paper sizes in PDF points. Matching is tolerant because producers
# often round millimetres or include a fraction of a point in the MediaBox.
PDF_PAGE_FORMATS: tuple[tuple[str, float, float], ...] = (
    ("A0", 2383.94, 3370.39),
    ("A1", 1683.78, 2383.94),
    ("A2", 1190.55, 1683.78),
    ("A3", 841.89, 1190.55),
    ("A4", 595.28, 841.89),
    ("A5", 419.53, 595.28),
    ("Letter", 612.0, 792.0),
    ("Legal", 612.0, 1008.0),
    ("Tabloid", 792.0, 1224.0),
)


def classify_pdf_page(width_points: float, height_points: float,
                      tolerance: float = 0.025) -> str:
    """Identify a common PDF page format and orientation from its MediaBox."""
    portrait = height_points >= width_points
    short, long = sorted((float(width_points), float(height_points)))
    for name, canonical_width, canonical_height in PDF_PAGE_FORMATS:
        if (abs(short - canonical_width) / canonical_width <= tolerance
                and abs(long - canonical_height) / canonical_height <= tolerance):
            orientation = "Portrait" if portrait else "Landscape"
            return f"{name} {orientation}"
    width_mm = width_points * 25.4 / 72.0
    height_mm = height_points * 25.4 / 72.0
    return f"Custom {width_mm:.0f} × {height_mm:.0f} mm"


def entries_for_paths(paths: list[Path], *, kinds: set[str]) -> list[Entry]:
    """Return metadata for an ordered, explicitly opened set of files.

    Missing, unsupported, and filtered-out paths are skipped. Unlike a folder
    scan, order is intentionally preserved so each newly opened document lands
    at the end of the session list. Files are de-duplicated by their resolved
    path without reading or altering their contents.
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        identity = os.path.normcase(str(path))
        if identity in seen:
            continue
        seen.add(identity)
        kind = suffix_kind(path.suffix)
        if kind is None or kind not in kinds:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(stat.st_mode):
            continue
        entries.append(Entry(path, stat.st_size, stat.st_mtime, kind))
    return entries


def _skip_dir(name: str) -> bool:
    """True for hidden and well-known noise directories."""
    return name.startswith(".") or name.lower() in SKIP_DIRS


def scan_folder(folder: Path, *, kinds: set[str], max_depth: int = 0,
                sort_by: str = "name", descending: bool = False,
                limit: int = MAX_SCAN_ENTRIES) -> tuple[list[Entry], bool]:
    """List supported files in ``folder``, optionally several levels deep.

    Parameters
    ----------
    max_depth:
        ``0``  - only files directly inside ``folder`` (the classic behaviour).
        ``n``  - also descend ``n`` levels of sub-folders.
        ``-1`` (:data:`DEPTH_UNLIMITED`) - walk the entire tree.
    limit:
        Stop after this many matches; the second return value is then ``True``.

    The walk is **breadth-first**, so the shallowest files are found first and
    a truncated scan still contains everything near the top rather than one
    deep branch. Uses :func:`os.scandir`, whose ``DirEntry.stat()`` is served
    from the directory enumeration on Windows: roughly an order of magnitude
    fewer syscalls than ``Path.iterdir()`` + ``Path.stat()``, which matters a
    lot on network drives and matters even more once the scan is recursive.

    Returns ``(entries, truncated)``.
    """
    root = Path(folder)
    entries: list[Entry] = []
    truncated = False
    seen_dirs: set[str] = set()       # realpath guard against symlink loops
    level: list[Path] = [root]        # directories at the current depth
    depth = 0

    while level and not truncated:
        deeper: list[Path] = []
        for current in level:
            try:
                real = os.path.realpath(current)
            except OSError:
                continue
            if real in seen_dirs:
                continue
            seen_dirs.add(real)
            try:
                with os.scandir(current) as it:
                    for item in it:
                        try:
                            if item.is_dir(follow_symlinks=False):
                                # Descend only while the next level is still
                                # within the requested depth.
                                if (max_depth < 0 or depth < max_depth) and not _skip_dir(item.name):
                                    deeper.append(Path(item.path))
                                continue
                            if not item.is_file(follow_symlinks=False):
                                continue
                            kind = suffix_kind(os.path.splitext(item.name)[1])
                            if kind is None or kind not in kinds:
                                continue
                            stat = item.stat()
                            entries.append(Entry(Path(item.path), stat.st_size,
                                                 stat.st_mtime, kind, root))
                            if len(entries) >= limit:
                                truncated = True
                                break
                        except OSError:
                            continue
            except OSError:
                continue
            if truncated:
                break
        depth += 1
        level = deeper

    entries.sort(key=SORT_KEYS.get(sort_by, SORT_KEYS["name"]), reverse=descending)
    return entries, truncated


def format_size(num_bytes: int) -> str:
    """Compact human-readable byte count."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def format_time(stamp: float) -> str:
    """Local timestamp, short form."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def _normalise(image: Image.Image) -> Image.Image:
    """Convert exotic modes into something Tk and the clipboard can handle."""
    mode = image.mode
    if mode in ("P", "PA"):
        return image.convert("RGBA")
    if mode == "CMYK":
        return image.convert("RGB")
    if mode in ("I", "I;16", "I;16B", "I;16L", "F"):
        # Scientific 16/32-bit data: stretch to 8-bit for display.
        return ImageOps.autocontrast(image.convert("F").convert("L"))
    if mode == "1":
        return image.convert("L")
    return image


def _resample_region(image: Image.Image, box: tuple[float, float, float, float],
                     out_size: tuple[int, int], fast: bool) -> Image.Image:
    """Crop ``box`` from ``image`` and scale it to ``out_size``.

    ``Image.resize(..., box=...)`` fuses the crop and the scale into a single
    pass, so only the requested region is ever touched. For heavy reductions a
    :meth:`Image.reduce` pre-pass (integer box filter, very fast) removes most
    of the pixels before the expensive filter runs.
    """
    left = max(0.0, min(box[0], image.width))
    top = max(0.0, min(box[1], image.height))
    right = max(left + 1e-6, min(box[2], image.width))
    bottom = max(top + 1e-6, min(box[3], image.height))
    out_w = max(1, int(out_size[0]))
    out_h = max(1, int(out_size[1]))

    src_w = right - left
    src_h = bottom - top
    ratio = min(src_w / out_w, src_h / out_h)

    work = image
    if ratio >= 3.0:
        factor = max(1, min(int(ratio), 16))
        int_box = (int(left), int(top), max(int(left) + factor, int(right)),
                   max(int(top) + factor, int(bottom)))
        int_box = (int_box[0], int_box[1],
                   min(int_box[2], image.width), min(int_box[3], image.height))
        try:
            work = image.reduce(factor, box=int_box)
            left = (left - int_box[0]) / factor
            top = (top - int_box[1]) / factor
            right = left + src_w / factor
            bottom = top + src_h / factor
        except ValueError:
            work = image

    if fast:
        resample = Image.Resampling.BILINEAR
    else:
        resample = (Image.Resampling.LANCZOS
                    if (right - left) > out_w else Image.Resampling.BICUBIC)
    return work.resize((out_w, out_h), resample,
                       box=(left, top, min(right, work.width), min(bottom, work.height)))


class Document:
    """Base interface shared by raster images and paged vector documents."""

    kind = KIND_IMAGE

    def __init__(self, entry: Entry) -> None:
        self.entry = entry
        self.page_count = 1
        self.info: dict[str, str] = {}

    # -- geometry ---------------------------------------------------------
    def page_size(self, page: int) -> tuple[int, int]:
        """Logical pixel size of a page at 100 % zoom."""
        raise NotImplementedError

    # -- rendering --------------------------------------------------------
    def render_region(self, page: int, box: tuple[float, float, float, float],
                      out_size: tuple[int, int], *, fast: bool = False) -> Image.Image:
        """Rasterise ``box`` (in page pixels at 100 %) into ``out_size`` pixels."""
        raise NotImplementedError

    def full_page(self, page: int, *, quality_scale: float = 1.0) -> Image.Image:
        """Return a whole page as a PIL image at ``quality_scale`` x 100 %."""
        width, height = self.page_size(page)
        out = (max(1, int(width * quality_scale)), max(1, int(height * quality_scale)))
        return self.render_region(page, (0, 0, width, height), out, fast=False)

    def pixel(self, page: int, x: int, y: int) -> Optional[tuple[int, ...]]:
        """Colour sample at a page pixel, or ``None`` if not available."""
        return None

    def text(self, page: int) -> str:
        """Extracted page text, empty for raster images."""
        return ""

    def words(self, page: int) -> list[tuple]:
        """Word boxes ``(x0, y0, x1, y1, text, block, line, word_no)`` in
        source pixels; empty for formats without a text layer."""
        return []

    def close(self) -> None:
        """Release any native resources."""


class RasterDocument(Document):
    """A bitmap image; multi-frame files expose one page per frame."""

    kind = KIND_IMAGE

    def __init__(self, entry: Entry, data: bytes) -> None:
        super().__init__(entry)
        import io as _io

        self._buffer = _io.BytesIO(data)      # kept alive; the file is not
        self._image = Image.open(self._buffer)  # locked because we own the bytes
        self.page_count = max(1, getattr(self._image, "n_frames", 1))
        self._frames: dict[int, Image.Image] = {}
        self._lock = threading.Lock()

        base = self._image
        self.info = {
            "Format": str(base.format or "?"),
            "Mode": base.mode,
            "Size": f"{base.width} x {base.height} px",
            "Pages": str(self.page_count),
        }
        dpi = base.info.get("dpi")
        if dpi:
            try:
                self.info["DPI"] = f"{float(dpi[0]):.0f} x {float(dpi[1]):.0f}"
            except Exception:
                pass
        # matplotlib and friends store provenance in PNG text chunks.
        for key in ("Software", "Title", "Author", "Description", "Comment",
                    "parameters", "Creation Time"):
            value = base.info.get(key)
            if isinstance(value, (str, bytes)) and value:
                text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                self.info[key] = text[:400]

    @property
    def dpi(self) -> Optional[tuple[float, float]]:
        """Source DPI if the file declares one."""
        value = self._image.info.get("dpi")
        if value:
            try:
                return (float(value[0]), float(value[1]))
            except Exception:
                return None
        return None

    def frame(self, page: int) -> Image.Image:
        """Return a decoded, normalised frame, caching the result."""
        index = max(0, min(page, self.page_count - 1))
        with self._lock:
            cached = self._frames.get(index)
            if cached is not None:
                return cached
            if self.page_count > 1:
                self._image.seek(index)
            frame = _normalise(self._image.copy() if self.page_count > 1 else self._image)
            frame.load()
            self._frames[index] = frame
            return frame

    def page_size(self, page: int) -> tuple[int, int]:
        image = self.frame(page)
        return image.width, image.height

    def render_region(self, page: int, box, out_size, *, fast: bool = False) -> Image.Image:
        return _resample_region(self.frame(page), box, out_size, fast)

    def full_page(self, page: int, *, quality_scale: float = 1.0) -> Image.Image:
        image = self.frame(page)
        if abs(quality_scale - 1.0) < 1e-9:
            return image                     # no copy, no resample: instant
        return super().full_page(page, quality_scale=quality_scale)

    def pixel(self, page: int, x: int, y: int):
        image = self.frame(page)
        if 0 <= x < image.width and 0 <= y < image.height:
            try:
                value = image.getpixel((x, y))
                return value if isinstance(value, tuple) else (value,)
            except Exception:
                return None
        return None

    def close(self) -> None:
        try:
            self._image.close()
        except Exception:
            pass
        self._frames.clear()


class VectorDocument(Document):
    """A PDF (or other PyMuPDF-supported format) rendered on demand.

    ``base_dpi`` defines what "100 %" means: the page is treated as a bitmap of
    ``points * base_dpi / 72`` pixels. Rendering always happens at the exact
    requested zoom, so the output is resolution independent.
    """

    kind = KIND_DOC

    def __init__(self, entry: Entry, data: bytes, base_dpi: float = 150.0) -> None:
        super().__init__(entry)
        _load_fitz()
        self.base_dpi = base_dpi
        self._doc = fitz.open(stream=data, filetype=entry.path.suffix.lstrip("."))
        self.page_count = max(1, self._doc.page_count)
        # Reentrant: render_region and text() call _page() while already
        # holding the lock, and the editing subclass in pdfedit.py runs
        # whole mutate-then-invalidate sequences under one acquisition.
        # PyMuPDF documents are not thread safe, so every fitz access -
        # geometry reads included - happens inside this lock; workers render
        # while the Tk thread mutates, and the lock serialises them.
        self._lock = threading.RLock()
        #: Bumped by the editing subclass after every content mutation.
        #: Cached rasterisations remember the version they were made from,
        #: so a stale tile can never be served after an edit.
        self.content_version = 0
        # Last rasterisation, rendered with a margin around the requested box.
        # Panning inside that margin is then a pure crop instead of a
        # re-rasterisation, which is the difference between 100 ms and 1 ms.
        self._tile: Optional[dict] = None
        meta = self._doc.metadata or {}
        self.info = {
            "Format": (meta.get("format") or entry.path.suffix.upper().lstrip(".")),
            "Pages": str(self.page_count),
            "Page format": self.page_format(0),
            "Render DPI": f"{base_dpi:.0f}",
        }
        for key in ("title", "author", "subject", "creator", "producer", "creationDate"):
            value = meta.get(key)
            if value:
                self.info[key.capitalize()] = str(value)[:400]

    @property
    def dpi(self) -> Optional[tuple[float, float]]:
        return (self.base_dpi, self.base_dpi)

    def _page(self, page: int):
        return self._doc.load_page(max(0, min(page, self.page_count - 1)))

    def invalidate_tiles(self) -> None:
        """Drop the padded raster cache so the next repaint re-rasterises."""
        with self._lock:
            self._tile = None

    def page_size(self, page: int) -> tuple[int, int]:
        with self._lock:
            rect = self._page(page).rect
        factor = self.base_dpi / 72.0
        return max(1, int(round(rect.width * factor))), max(1, int(round(rect.height * factor)))

    def page_size_points(self, page: int) -> tuple[float, float]:
        """Physical page dimensions in PDF points (1/72 inch)."""
        with self._lock:
            rect = self._page(page).rect
        return float(rect.width), float(rect.height)

    def page_format(self, page: int) -> str:
        """Common paper name plus orientation, or measured custom dimensions."""
        return classify_pdf_page(*self.page_size_points(page))

    def render_region(self, page: int, box, out_size, *, fast: bool = False) -> Image.Image:
        out_w = max(1, int(out_size[0]))
        out_h = max(1, int(out_size[1]))
        width, height = self.page_size(page)
        left = max(0.0, min(box[0], width))
        top = max(0.0, min(box[1], height))
        right = max(left + 1.0, min(box[2], width))
        bottom = max(top + 1.0, min(box[3], height))
        scale = out_w / (right - left)

        cached = self._crop_from_tile(page, (left, top, right, bottom), (out_w, out_h), scale)
        if cached is not None:
            return cached

        # Rasterise with a margin so the next pan is served from the cache. The
        # margin is snapped to a whole number of output pixels, which keeps
        # later crops pixel-aligned and therefore perfectly sharp.
        margin_x = max(0.0, min((right - left) * 0.30, left)) if left > 0 else 0.0
        margin_y = max(0.0, min((bottom - top) * 0.30, top)) if top > 0 else 0.0
        margin_x = round(margin_x * scale) / scale
        margin_y = round(margin_y * scale) / scale
        p_left = max(0.0, left - margin_x)
        p_top = max(0.0, top - margin_y)
        p_right = min(float(width), right + (right - left) * 0.30)
        p_bottom = min(float(height), bottom + (bottom - top) * 0.30)

        factor = self.base_dpi / 72.0
        clip = fitz.Rect(p_left / factor, p_top / factor, p_right / factor, p_bottom / factor)
        matrix = fitz.Matrix(scale * factor, scale * factor)
        with self._lock:
            # The version is captured under the same lock as the pixels, so a
            # tile rendered just before an edit can never masquerade as fresh.
            version = self.content_version
            pix = self._page(page).get_pixmap(
                matrix=matrix, clip=clip, alpha=False,
                colorspace=fitz.csRGB, annots=True,
            )
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if image.width * image.height <= 24_000_000:   # ~72 MB RGB, keep it bounded
            self._tile = {"page": page, "scale": image.width / max(p_right - p_left, 1e-6),
                          "box": (p_left, p_top, p_right, p_bottom), "image": image,
                          "version": version}
        result = self._crop_from_tile(page, (left, top, right, bottom), (out_w, out_h), scale)
        if result is not None:
            return result
        return image.resize((out_w, out_h), Image.Resampling.BILINEAR)

    def _crop_from_tile(self, page: int, box, out_size, scale: float) -> Optional[Image.Image]:
        """Serve a request from the padded cache when it is covered by it."""
        tile = self._tile
        # A 1 % shortfall in resolution is invisible and avoids cache misses
        # caused purely by rounding the output size to whole pixels.
        if tile is None or tile["page"] != page or tile["scale"] < scale * 0.99:
            return None
        if tile.get("version", 0) != self.content_version:
            return None
        left, top, right, bottom = box
        t_left, t_top, t_right, t_bottom = tile["box"]
        if left < t_left - 0.5 or top < t_top - 0.5 or right > t_right + 0.5 or bottom > t_bottom + 0.5:
            return None
        factor = tile["scale"]
        crop = ((left - t_left) * factor, (top - t_top) * factor,
                (right - t_left) * factor, (bottom - t_top) * factor)
        image = tile["image"]
        crop = (max(0.0, crop[0]), max(0.0, crop[1]),
                min(float(image.width), crop[2]), min(float(image.height), crop[3]))
        resample = (Image.Resampling.LANCZOS if factor > scale * 1.02
                    else Image.Resampling.BILINEAR)
        return image.resize(out_size, resample, box=crop)

    def text(self, page: int) -> str:
        try:
            with self._lock:
                return self._page(page).get_text("text")
        except Exception:
            return ""

    def words(self, page: int) -> list[tuple]:
        """Word boxes in source pixels (``points * base_dpi / 72``).

        The textpage is created and consumed inside the lock, on the one page
        proxy that produced it; only plain tuples leave the lock scope.
        """
        try:
            with self._lock:
                raw = self._page(page).get_text("words")
        except Exception:
            return []
        factor = self.base_dpi / 72.0
        return [(x0 * factor, y0 * factor, x1 * factor, y1 * factor,
                 str(text), int(block), int(line), int(word_no))
                for x0, y0, x1, y1, text, block, line, word_no in raw]

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass


def load_document(entry: Entry, *, pdf_dpi: float = 150.0) -> Document:
    """Read a file into memory and wrap it in the right document class."""
    if entry.kind == KIND_VIDEO:
        raise TypeError("Videos are streamed by VideoSession, not loaded as documents")
    data = entry.path.read_bytes()
    if entry.kind == KIND_DOC:
        return VectorDocument(entry, data, base_dpi=pdf_dpi)
    return RasterDocument(entry, data)


class DocumentCache:
    """Tiny LRU cache of open documents keyed by path *and* file stamp.

    Because the key includes size and mtime, an overwritten figure is never
    served from the cache: hot reload is automatic and free.
    """

    def __init__(self, max_items: int = 6) -> None:
        self.max_items = max_items
        self._items: dict[tuple, Document] = {}
        self._order: list[tuple] = []
        self._lock = threading.Lock()

    def get(self, entry: Entry, *, pdf_dpi: float = 150.0) -> Document:
        key = entry.key + (pdf_dpi if entry.kind == KIND_DOC else 0,)
        with self._lock:
            document = self._items.get(key)
            if document is not None:
                self._order.remove(key)
                self._order.append(key)
                return document
        document = load_document(entry, pdf_dpi=pdf_dpi)
        evicted: list[Document] = []
        with self._lock:
            if key not in self._items:
                self._items[key] = document
                self._order.append(key)
                while len(self._order) > self.max_items:
                    old = self._order.pop(0)
                    stale = self._items.pop(old, None)
                    if stale is not None:
                        evicted.append(stale)
            else:
                evicted.append(document)
                document = self._items[key]
        for stale in evicted:
            stale.close()
        return document

    def peek(self, entry: Entry, *, pdf_dpi: float = 150.0) -> Optional[Document]:
        """Return an already-loaded document without touching the disk."""
        key = entry.key + (pdf_dpi if entry.kind == KIND_DOC else 0,)
        with self._lock:
            return self._items.get(key)

    def evict(self, path: Path) -> int:
        """Close and drop every cached document for ``path``.

        Called after an edited PDF is saved: the bytes on disk no longer match
        what any cached document was opened from, whatever its stamp says
        (stamps have mtime granularity; a fast save can leave them equal).
        """
        target = str(path)
        with self._lock:
            stale_keys = [key for key in self._order if key[0] == target]
            stale = [self._items.pop(key) for key in stale_keys]
            for key in stale_keys:
                self._order.remove(key)
        for document in stale:
            document.close()
        return len(stale)

    def clear(self) -> None:
        with self._lock:
            documents = list(self._items.values())
            self._items.clear()
            self._order.clear()
        for document in documents:
            document.close()


def make_thumbnail(document: Document, size: int) -> Image.Image:
    """Render a square-bounded thumbnail of page 0."""
    width, height = document.page_size(0)
    scale = min(size / max(width, 1), size / max(height, 1), 1.0)
    out = (max(1, int(width * scale)), max(1, int(height * scale)))
    image = document.render_region(0, (0, 0, width, height), out, fast=False)
    return image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image


def thumbnail_for(entry: Entry, size: int) -> Image.Image:
    """Build a thumbnail without polluting the document cache.

    JPEGs use :meth:`Image.draft`, which lets libjpeg decode at 1/2, 1/4 or 1/8
    scale directly - typically 5-10x faster than a full decode. PDFs are
    rendered at a low DPI for the same reason.
    """
    if entry.kind == KIND_VIDEO:
        from . import video as V

        return V.thumbnail(entry.path, size)

    if entry.kind == KIND_DOC:
        document = load_document(entry, pdf_dpi=72.0)
        try:
            return make_thumbnail(document, size)
        finally:
            document.close()

    import io as _io

    data = entry.path.read_bytes()
    with Image.open(_io.BytesIO(data)) as image:
        try:
            image.draft("RGB", (size * 2, size * 2))
        except Exception:
            pass
        frame = _normalise(image)
        frame.thumbnail((size, size), Image.Resampling.BILINEAR, reducing_gap=2.0)
        return frame.copy()


class Worker:
    """A small thread pool whose results are delivered on the Tk main loop.

    Tk is not thread safe, so background threads never touch widgets. They push
    ``(callback, result, error)`` tuples onto a queue that the UI drains from a
    periodic ``after`` tick.
    """

    def __init__(self, root, max_workers: int = 3, poll_ms: int = 25) -> None:
        self.root = root
        self.poll_ms = poll_ms
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="fv-worker")
        # Speculative work must never occupy every worker needed by an open
        # request. A single background decoder also limits peak image memory.
        self._background_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fv-background")
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._background_queue: "queue.Queue[tuple]" = queue.Queue()
        self._alive = True
        self._drain_job = self.root.after(self.poll_ms, self._drain)

    def submit(self, job: Callable[[], object], done: Callable[[object], None],
               failed: Optional[Callable[[BaseException], None]] = None, *,
               background: bool = False,
               valid: Optional[Callable[[], bool]] = None) -> Future:
        """Run work off-thread and deliver its callback on the UI thread.

        ``background`` reserves the foreground pool for user requests. ``valid``
        skips obsolete jobs before expensive work and suppresses stale results.
        It runs on both threads, so it must only read thread-safe Python state,
        never Tk widgets. The returned future can cancel work still in the queue.
        """
        if not self._alive:
            cancelled = Future()
            cancelled.cancel()
            return cancelled

        results = self._background_queue if background else self._queue

        def run() -> object:
            try:
                if not self._alive or (valid is not None and not valid()):
                    return None
                result = job()
            except BaseException as exc:  # noqa: BLE001 - forwarded to the UI
                if self._alive:
                    results.put((failed, exc, True, valid))
                return None
            else:
                if self._alive:
                    results.put((done, result, False, valid))
                return result

        pool = self._background_pool if background else self._pool
        return pool.submit(run)

    def _drain(self) -> None:
        """Deliver finished jobs; always reschedules itself.

        A callback that raises must never break the delivery loop, otherwise
        the whole app would silently stop receiving background results.
        """
        import traceback

        deadline = time.perf_counter() + 0.008   # keep the UI thread responsive
        try:
            while time.perf_counter() < deadline:
                try:
                    # An already-rendered thumbnail backlog must not delay a
                    # completed foreground open request on the UI thread either.
                    item = self._queue.get_nowait()
                except queue.Empty:
                    try:
                        item = self._background_queue.get_nowait()
                    except queue.Empty:
                        break
                callback, payload, is_error, valid = item
                if callback is None:
                    continue
                try:
                    if self._alive and (valid is None or valid()):
                        callback(payload)
                except Exception:
                    traceback.print_exc()
        finally:
            if self._alive:
                try:
                    self._drain_job = self.root.after(self.poll_ms, self._drain)
                except Exception:
                    self._alive = False

    def shutdown(self) -> None:
        self._alive = False
        try:
            self.root.after_cancel(self._drain_job)
        except Exception:
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._background_pool.shutdown(wait=False, cancel_futures=True)
        # Discard completed images and bound their lifetime after a window closes.
        for results in (self._queue, self._background_queue):
            while True:
                try:
                    results.get_nowait()
                except queue.Empty:
                    break
