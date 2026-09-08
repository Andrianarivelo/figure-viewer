"""Headless OCR pipeline: language packs, scan preprocessing, page OCR,
invisible text overlays and quality metrics.

Design rules (Section K of the 3.5 design, enforced by ``tests/test_ocr.py``)
-----------------------------------------------------------------------------
1. **No tkinter.** Everything here is fully testable headless; all UI glue
   lives elsewhere.
2. **Optional dependencies are probed lazily.** PyMuPDF is reached through
   :mod:`figureviewer.docs` (``D.fitz``); OpenCV is imported only when
   preprocessing actually runs and its absence degrades gracefully to
   grayscale plus upscale, never to an import-time failure.
3. **Page proxy binding.** A PyMuPDF textpage / pixmap request and every
   object derived from a ``Page`` is used strictly while that exact page
   proxy is alive and under the document lock; results cross the lock
   boundary only as plain Python data (bytes, tuples, strings).
4. **The raw file is sacred.** Nothing here writes to a user file; OCR
   overlays mutate only the in-memory session document, wrapped in ONE
   :meth:`EditSession.checkpoint` so a single Ctrl+Z reverts the whole run
   and an aborted run restores the exact before state.
5. **Never auto-download.** :meth:`PackManager.download` runs only on an
   explicit user gesture; every failure carries the manual escape hatch
   (the URL to fetch and the directory to drop it into), so the feature is
   fully usable offline.

Invisible text layer: words are inserted with ``render_mode=3`` (neither
fill nor stroke), so a page's rendered pixels stay byte-identical while
``get_text`` and ``search_for`` start working. Original page objects are
never rasterised and replaced.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from PIL import Image

from . import docs as D

# ---------------------------------------------------------------------------
# Tunables (kept together so behaviour is auditable at a glance).

#: A page with at least this many native words is never overlaid again.
NATIVE_WORD_SKIP = 10
#: Below this many native words a page counts as having no text layer.
NATIVE_WORD_TEXTLESS = 3
#: Fraction of the page area images must cover before a page "needs OCR".
IMAGE_COVERAGE_MIN = 0.5
#: Preprocessing upscales so the effective resolution is at least this.
TARGET_OCR_DPI = 300
#: Deskew is applied only within this angle band (degrees). Below it the
#: rotation is noise; above it the "skew" is almost certainly page content.
DESKEW_MIN_DEG = 0.3
DESKEW_MAX_DEG = 15.0
#: Download stream chunk size.
_CHUNK = 64 * 1024

VARIANTS = ("fast", "best")

TESSDATA_URL_TEMPLATES = {
    "fast": "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{code}.traineddata",
    "best": "https://github.com/tesseract-ocr/tessdata_best/raw/main/{code}.traineddata",
}

#: Curated language menu: code -> (display name, approximate download MB for
#: the fast variant). Sizes are for the picker UI only, never enforced.
KNOWN_LANGS: dict[str, tuple[str, float]] = {
    "eng": ("English", 4.0),
    "fra": ("French", 1.1),
    "deu": ("German", 1.5),
    "spa": ("Spanish", 1.2),
    "ita": ("Italian", 1.2),
    "por": ("Portuguese", 1.2),
    "nld": ("Dutch", 1.2),
    "pol": ("Polish", 1.6),
    "rus": ("Russian", 2.6),
    "ukr": ("Ukrainian", 2.5),
    "tur": ("Turkish", 1.6),
    "ara": ("Arabic", 1.4),
    "heb": ("Hebrew", 1.2),
    "hin": ("Hindi", 1.1),
    "jpn": ("Japanese", 2.3),
    "kor": ("Korean", 1.6),
    "chi_sim": ("Chinese (Simplified)", 2.4),
    "chi_tra": ("Chinese (Traditional)", 2.7),
    "vie": ("Vietnamese", 1.1),
    "tha": ("Thai", 1.0),
}


class OcrPackError(RuntimeError):
    """A language pack is missing or could not be downloaded. The message is
    user-facing and always names the manual escape hatch."""


class _OcrCancelled(Exception):
    """Internal control flow: a cancel event fired between pages. Raised
    inside :meth:`EditSession.checkpoint` so the checkpoint restores the
    document, then swallowed by :func:`make_searchable`."""


def _require_fitz() -> None:
    if not D.HAVE_FITZ:
        raise RuntimeError(
            "OCR requires PyMuPDF.\n\nInstall it with:\n"
            "    pip install pymupdf"
        )


def default_tessdata_root() -> Path:
    """The app-owned language pack root, mirroring ``app.SETTINGS_DIR``.

    Duplicated (not imported) because ``figureviewer.app`` imports tkinter
    and this module must stay headless.
    """
    return Path.home() / "AppData" / "Roaming" / "FigureViewerApp" / "tessdata"


def find_language_dir(base: Path | str, lang: str,
                      variant: str = "fast") -> Optional[Path]:
    """Directory that actually contains ``<lang>.traineddata``, or ``None``.

    Accepts either the pack root (``root/<variant>/<lang>.traineddata``) or a
    directory holding the file directly; the flat root is also accepted
    because the step-0 spike left ``eng.traineddata`` there.
    """
    root = Path(base)
    for candidate in (root / variant, root):
        try:
            if (candidate / f"{lang}.traineddata").is_file():
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Availability probe

_available_cache: dict[str, bool] = {}


def ocr_available(tessdata_dir: Optional[Path | str] = None) -> bool:
    """Whether the installed PyMuPDF wheel carries integrated Tesseract and a
    usable ``eng`` pack. Cached per resolved pack directory; a tiny 1x1 white
    pixmap is OCRed once, and every failure is a clean ``False``."""
    if not D.HAVE_FITZ:
        return False
    if tessdata_dir is not None:
        eng_dir = find_language_dir(tessdata_dir, "eng", "fast")
        if eng_dir is None:
            return False           # an explicit dir without eng is a clean no
    else:
        # Default root, else let PyMuPDF discover TESSDATA_PREFIX itself.
        eng_dir = find_language_dir(default_tessdata_root(), "eng", "fast")
    key = str(eng_dir) if eng_dir is not None else "<engine-default>"
    cached = _available_cache.get(key)
    if cached is not None:
        return cached
    fitz = D.fitz
    ok = False
    try:
        pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 1, 1), 0)
        pix.clear_with(255)
        kwargs = {"tessdata": str(eng_dir)} if eng_dir is not None else {}
        pix.pdfocr_tobytes(language="eng", **kwargs)
        ok = True
    except Exception:
        ok = False
    _available_cache[key] = ok
    return ok


# ---------------------------------------------------------------------------
# Language pack management


def _urlopen(url: str):
    """Open a streaming HTTP response. A module-level seam so tests mock the
    network without touching the stdlib."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "FigView"})
    return urllib.request.urlopen(request, timeout=30)


class PackManager:
    """Tessdata layout and download bookkeeping under one root directory.

    Layout: ``root/<variant>/<lang>.traineddata`` for variant in ``fast`` /
    ``best``, plus ``root/packs.json``. Construction adopts any flat
    ``root/<lang>.traineddata`` into ``fast/`` (the step-0 spike left ``eng``
    flat). Takes a plain directory, never an App reference, so it stays
    headless-testable; the Tk glue passes ``default_tessdata_root()``.
    """

    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir)
        self.migrate()

    # -- layout -----------------------------------------------------------
    def pack_dir(self, variant: str) -> Path:
        if variant not in VARIANTS:
            raise ValueError(f"unknown tessdata variant {variant!r}")
        return self.root / variant

    def migrate(self) -> list[str]:
        """Adopt flat ``root/<lang>.traineddata`` files into ``fast/``.

        Copies (rather than moves) via a ``.part`` staging file so a crash
        can never corrupt a pack and the flat original keeps working for any
        older code path still probing the flat layout. Idempotent.
        """
        adopted: list[str] = []
        if not self.root.is_dir():
            return adopted
        try:
            flat = sorted(self.root.glob("*.traineddata"))
        except OSError:
            return adopted
        fast = self.root / "fast"
        for source in flat:
            target = fast / source.name
            if target.exists():
                continue
            part = target.with_name(target.name + ".part")
            try:
                fast.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, part)
                os.replace(part, target)
            except OSError:
                try:
                    part.unlink()
                except OSError:
                    pass
                continue
            lang = source.name[: -len(".traineddata")]
            adopted.append(lang)
            self._record(lang, "fast", "migrated from flat layout",
                         target.stat().st_size)
        return adopted

    def installed(self, variant: str) -> list[str]:
        """Language codes present for a variant, by directory scan."""
        directory = self.pack_dir(variant)
        try:
            names = sorted(p.name for p in directory.glob("*.traineddata"))
        except OSError:
            return []
        return [name[: -len(".traineddata")] for name in names]

    def has(self, lang: str, variant: str = "fast") -> bool:
        return (self.pack_dir(variant) / f"{lang}.traineddata").is_file()

    # -- packs.json bookkeeping -------------------------------------------
    @property
    def packs_path(self) -> Path:
        return self.root / "packs.json"

    def packs(self) -> dict:
        """The bookkeeping dict, ``{"<variant>/<lang>": {...}}``. A corrupt
        or missing file reads as empty rather than crashing."""
        try:
            data = json.loads(self.packs_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _record(self, lang: str, variant: str, source_url: str,
                nbytes: int) -> None:
        data = self.packs()
        data[f"{variant}/{lang}"] = {
            "lang": lang,
            "variant": variant,
            "source_url": source_url,
            "bytes": int(nbytes),
            "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = self.packs_path.with_name("packs.json.part")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.packs_path)
        except OSError:
            pass                    # bookkeeping must never break a download

    # -- downloads ---------------------------------------------------------
    def download(self, code: str, variant: str = "fast",
                 progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
                 cancel=None) -> Path:
        """Stream one language pack to disk. NEVER call without an explicit
        user gesture; the app must stay fully offline-safe.

        Streams 64 KB chunks to ``<lang>.traineddata.part`` and only then
        ``os.replace``s it into place, so a killed download can never leave a
        corrupt pack. ``progress_cb(bytes_done, total_or_None)``; ``cancel``
        is an object with ``is_set()`` (a ``threading.Event``), checked
        between chunks. Every failure, cancellation included, raises
        :class:`OcrPackError` naming the manual escape hatch.
        """
        url = TESSDATA_URL_TEMPLATES[variant].format(code=code)
        target_dir = self.pack_dir(variant)
        final = target_dir / f"{code}.traineddata"
        part = target_dir / f"{code}.traineddata.part"
        escape_hatch = (f"You can also download\n    {url}\n"
                        f"yourself and drop it into\n    {target_dir}")
        done = 0
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with _urlopen(url) as response:
                total: Optional[int] = None
                headers = getattr(response, "headers", None)
                if headers is not None:
                    try:
                        total = int(headers.get("Content-Length") or 0) or None
                    except (TypeError, ValueError):
                        total = None
                with open(part, "wb") as sink:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise _OcrCancelled()
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        sink.write(chunk)
                        done += len(chunk)
                        if progress_cb is not None:
                            progress_cb(done, total)
            if done == 0:
                raise OSError("the server returned an empty file")
            os.replace(part, final)
        except _OcrCancelled:
            self._discard(part)
            raise OcrPackError(
                f"Download of the '{code}' ({variant}) pack was cancelled.\n\n"
                + escape_hatch
            ) from None
        except Exception as exc:
            self._discard(part)
            raise OcrPackError(
                f"Could not download the '{code}' ({variant}) language pack.\n"
                f"Reason: {exc}\n\n" + escape_hatch
            ) from exc
        self._record(code, variant, url, done)
        return final

    @staticmethod
    def _discard(part: Path) -> None:
        try:
            part.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Preprocessing (the measurable accuracy edge) and its invertible transform


@dataclass(frozen=True)
class Transform:
    """The exact affine :func:`preprocess` applied, as a forward 2x3 matrix
    mapping ORIGINAL render pixels to PROCESSED pixels:

    ``x' = a*x + b*y + c`` and ``y' = d*x + e*y + f`` for
    ``matrix = (a, b, c, d, e, f)``.

    Affine only (uniform per-axis scale plus a rotation about the image
    center); :meth:`invert_point` maps OCR word boxes on the processed
    bitmap back to original render pixels with sub-pixel accuracy.
    """

    matrix: tuple[float, float, float, float, float, float]

    @classmethod
    def identity(cls, scale_x: float = 1.0,
                 scale_y: Optional[float] = None) -> "Transform":
        sy = scale_x if scale_y is None else scale_y
        return cls((float(scale_x), 0.0, 0.0, 0.0, float(sy), 0.0))

    @classmethod
    def rotate_scale(cls, angle_deg: float, center: tuple[float, float],
                     scale_x: float = 1.0,
                     scale_y: Optional[float] = None) -> "Transform":
        """Rotation about ``center`` (cv2.getRotationMatrix2D convention,
        applied at the original size) followed by an axis-aligned scale."""
        sy = scale_x if scale_y is None else scale_y
        alpha = math.cos(math.radians(angle_deg))
        beta = math.sin(math.radians(angle_deg))
        cx, cy = center
        rot = (alpha, beta, (1.0 - alpha) * cx - beta * cy,
               -beta, alpha, beta * cx + (1.0 - alpha) * cy)
        return cls((scale_x * rot[0], scale_x * rot[1], scale_x * rot[2],
                    sy * rot[3], sy * rot[4], sy * rot[5]))

    def apply_point(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.matrix
        return (a * x + b * y + c, d * x + e * y + f)

    def invert_point(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.matrix
        det = a * e - b * d
        if abs(det) < 1e-12:
            raise ValueError("transform is singular and cannot be inverted")
        px, py = x - c, y - f
        return ((e * px - b * py) / det, (a * py - d * px) / det)


_cv2_cache: Optional[tuple] = None
_cv2_probed = False


def preprocess_available() -> bool:
    """Whether OpenCV is discoverable without importing it (video.py idiom)."""
    return importlib.util.find_spec("cv2") is not None


def _opencv() -> Optional[tuple]:
    """Lazily import ``(cv2, numpy)`` once; ``None`` when unavailable."""
    global _cv2_cache, _cv2_probed
    if _cv2_probed:
        return _cv2_cache
    _cv2_probed = True
    if (importlib.util.find_spec("cv2") is None
            or importlib.util.find_spec("numpy") is None):
        _cv2_cache = None
        return None
    try:
        import cv2
        import numpy
        _cv2_cache = (cv2, numpy)
    except Exception:
        _cv2_cache = None
    return _cv2_cache


def _is_bimodal(gray, np) -> bool:
    """A histogram is "cleanly bimodal" when nearly all mass sits at the
    extremes (already black-on-white); adaptive thresholding such an image
    only erodes glyph edges, so it is skipped."""
    hist = np.bincount(gray.ravel(), minlength=256)
    total = int(hist.sum())
    if total == 0:
        return True
    extreme = int(hist[:64].sum()) + int(hist[192:].sum())
    return extreme / total >= 0.95


def _fold_angle(angle: float) -> float:
    """Normalize any estimate into [-45, 45] degrees."""
    while angle > 45.0:
        angle -= 90.0
    while angle < -45.0:
        angle += 90.0
    return angle


def _estimate_skew(binary, cv2, np) -> float:
    """Skew estimate in degrees from the Otsu-inverted ink mask.

    Primary: the angle of ``cv2.minAreaRect`` over all ink pixels. Cross
    check: the median angle of near-horizontal ``HoughLinesP`` segments
    (text baselines). When both exist and agree within 2 degrees their mean
    is used; on disagreement the Hough estimate wins, because a text page's
    line structure is a far better witness than its ink bounding box.
    """
    coords = cv2.findNonZero(binary)
    if coords is None:
        return 0.0
    rect_angle = _fold_angle(float(cv2.minAreaRect(coords)[2]))
    height, width = binary.shape[:2]
    hough: Optional[float] = None
    lines = cv2.HoughLinesP(binary, 1, math.pi / 180.0, threshold=120,
                            minLineLength=max(width // 4, 40), maxLineGap=8)
    if lines is not None and len(lines):
        angles = []
        for line in lines[:200]:
            x1, y1, x2, y2 = (float(v) for v in line[0])
            theta = _fold_angle(math.degrees(math.atan2(y2 - y1, x2 - x1)))
            if abs(theta) <= 20.0:
                angles.append(theta)
        if angles:
            hough = float(np.median(np.asarray(angles)))
    if hough is None:
        return rect_angle
    if abs(hough - rect_angle) <= 2.0:
        return (hough + rect_angle) / 2.0
    return hough


def preprocess(image: Image.Image, src_dpi: float,
               target_dpi: float = TARGET_OCR_DPI
               ) -> tuple[Image.Image, Transform]:
    """Prepare a rendered page bitmap for Tesseract.

    With OpenCV: grayscale -> ``fastNlMeansDenoising(h=10)`` -> deskew
    (Otsu-inverted mask, ``minAreaRect`` cross-checked against the median
    ``HoughLinesP`` angle, applied with ``warpAffine`` and white borders only
    when 0.3 <= |angle| <= 15 degrees) -> ``adaptiveThreshold`` (GAUSSIAN,
    blockSize=31, C=15; skipped when the histogram is already cleanly
    bimodal) -> INTER_CUBIC upscale so the effective dpi reaches
    ``target_dpi``. Without OpenCV: grayscale plus the same upscale, with an
    identity-plus-scale :class:`Transform`.

    Returns ``(processed_image, transform)`` where ``transform`` records the
    EXACT affine applied, for mapping OCR boxes back via ``invert_point``.
    """
    gray_img = image if image.mode == "L" else image.convert("L")
    scale = max(1.0, float(target_dpi) / max(float(src_dpi), 1.0))
    loaded = _opencv()
    if loaded is None:
        if scale <= 1.0:
            return gray_img, Transform.identity()
        new_w = max(1, round(gray_img.width * scale))
        new_h = max(1, round(gray_img.height * scale))
        out = gray_img.resize((new_w, new_h), Image.Resampling.BICUBIC)
        return out, Transform.identity(new_w / gray_img.width,
                                       new_h / gray_img.height)
    cv2, np = loaded

    arr = np.asarray(gray_img, dtype=np.uint8)
    arr = cv2.fastNlMeansDenoising(arr, None, 10, 7, 21)
    height, width = arr.shape[:2]

    rot = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    _, binary = cv2.threshold(arr, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    angle = _estimate_skew(binary, cv2, np)
    if DESKEW_MIN_DEG <= abs(angle) <= DESKEW_MAX_DEG:
        center = ((width - 1) / 2.0, (height - 1) / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        arr = cv2.warpAffine(arr, matrix, (width, height),
                             flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        # Record cv2's own matrix verbatim so invert_point is exact.
        rot = (float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[0, 2]),
               float(matrix[1, 0]), float(matrix[1, 1]), float(matrix[1, 2]))

    if not _is_bimodal(arr, np):
        arr = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 15)

    sx = sy = 1.0
    if scale > 1.0:
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        arr = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
        sx, sy = new_w / width, new_h / height

    transform = Transform((sx * rot[0], sx * rot[1], sx * rot[2],
                           sy * rot[3], sy * rot[4], sy * rot[5]))
    return Image.fromarray(arr), transform


# ---------------------------------------------------------------------------
# Page OCR


@dataclass(frozen=True)
class OcrWord:
    """One recognised word in PDF page points (top-left origin).

    ``conf`` is -1.0 when the engine reports no confidence: PyMuPDF's
    integrated Tesseract route exposes none, and inventing one would violate
    the no-hallucination rule.
    """

    rect_pts: tuple[float, float, float, float]
    text: str
    conf: float = -1.0
    #: Reading-order indices from the OCR layout (-1 when unknown), so a
    #: selection layer can group words into lines without re-clustering.
    block: int = -1
    line: int = -1


@dataclass(frozen=True)
class ImageWord:
    """One recognised word on a raster image, in image pixels."""

    rect_px: tuple[float, float, float, float]
    text: str
    block: int = -1
    line: int = -1


#: Assumed resolution for images that declare none (screen captures).
ASSUMED_IMAGE_DPI = 96.0
#: Longest side the processed OCR bitmap may reach for a raster image.
OCR_MAX_SIDE_PX = 4500


def image_ocr_dpi(size: tuple[int, int],
                  declared: Optional[Sequence[float]] = None
                  ) -> tuple[float, float]:
    """``(source_dpi, target_dpi)`` for OCRing a raster image.

    Declared metadata between 50 and 1200 dpi is trusted; anything else
    falls back to :data:`ASSUMED_IMAGE_DPI`. The upscale target starts at
    :data:`TARGET_OCR_DPI` but is capped so the processed bitmap's longest
    side stays within :data:`OCR_MAX_SIDE_PX`, and never drops below the
    source resolution (preprocessing only ever upsamples).
    """
    source = None
    if declared:
        try:
            value = float(declared[0])
        except (TypeError, ValueError, IndexError):
            value = 0.0
        if 50.0 <= value <= 1200.0:
            source = value
    if source is None:
        source = ASSUMED_IMAGE_DPI
    longest = max(1, max(size))
    target = max(float(TARGET_OCR_DPI), source)
    cap = source * OCR_MAX_SIDE_PX / longest
    if cap < target:
        target = max(source, cap)
    return source, target


def _resolve_pack_dir(tessdata_dir: Path | str, lang: str,
                      variant: str) -> Path:
    found = find_language_dir(tessdata_dir, lang, variant)
    if found is not None:
        return found
    url = TESSDATA_URL_TEMPLATES.get(variant, TESSDATA_URL_TEMPLATES["fast"])
    url = url.format(code=lang)
    expected = Path(tessdata_dir) / variant
    raise OcrPackError(
        f"The '{lang}' ({variant}) language pack is not installed under\n"
        f"    {tessdata_dir}\n"
        f"Install it via Tools > OCR > Language packs, or download\n"
        f"    {url}\nyourself and drop it into\n    {expected}"
    )


def page_ocr(document, page: int, *, lang: str = "eng",
             variant: str = "fast", preprocess_on: bool = True,
             dpi: int = 300, tessdata_dir: Path | str) -> list[OcrWord]:
    """OCR one page of an open :class:`~figureviewer.docs.VectorDocument`.

    Preprocessed route (the default; guarantees Tesseract sees OUR bitmap):
    render the page at ``dpi`` under the document lock, run
    :func:`preprocess`, wrap the processed PNG in a ``fitz.Pixmap`` and call
    ``pdfocr_tobytes``; each word box from the one-page OCR result is mapped
    back: OCR pdf points -> processed pixels (measured against the OCR
    page's rect, because the PNG's dpi metadata sets that rect) ->
    ``Transform.invert_point`` -> original render pixels -> page points.

    Baseline route (``preprocess_on=False``; also the benchmark control):
    ``page.get_textpage_ocr(flags=0, language=lang, dpi=dpi, full=True)``
    with ``tessdata`` pointed at the pack directory; the textpage already
    reports page-point coordinates.
    """
    _require_fitz()
    fitz = D.fitz
    pack_dir = _resolve_pack_dir(tessdata_dir, lang, variant)

    if not preprocess_on:
        with document._lock:
            # The textpage is bound to this exact page proxy: both live and
            # die inside this lock scope, and only plain tuples leave it.
            proxy = document._page(page)
            textpage = proxy.get_textpage_ocr(flags=0, language=lang, dpi=dpi,
                                              full=True,
                                              tessdata=str(pack_dir))
            raw = [(float(w[0]), float(w[1]), float(w[2]), float(w[3]),
                    str(w[4])) for w in textpage.extractWORDS()]
            del textpage, proxy
        return [OcrWord((x0, y0, x1, y1), text) for x0, y0, x1, y1, text in raw]

    with document._lock:
        proxy = document._page(page)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = proxy.get_pixmap(matrix=matrix, colorspace=fitz.csRGB,
                               alpha=False)
        samples = bytes(pix.samples)
        size = (pix.width, pix.height)
        del pix, proxy
    rendered = Image.frombytes("RGB", size, samples)
    bitmap_words = _ocr_bitmap(rendered, float(dpi), lang=lang,
                               pack_dir=pack_dir, preprocess_on=True,
                               target_dpi=max(dpi, TARGET_OCR_DPI))
    to_page_pts = 72.0 / float(dpi)
    return [OcrWord(tuple(v * to_page_pts for v in w.rect_px), w.text,
                    block=w.block, line=w.line) for w in bitmap_words]


def _ocr_bitmap(rendered: Image.Image, src_dpi: float, *, lang: str,
                pack_dir: Path, preprocess_on: bool,
                target_dpi: float) -> list[ImageWord]:
    """Run the integrated engine on one RGB bitmap.

    Word boxes come back in the bitmap's own pixel frame: OCR pdf points ->
    processed pixels (measured against the OCR page's rect, because the
    PNG's dpi metadata sets that rect) -> :meth:`Transform.invert_point` ->
    original bitmap pixels. Shared by :func:`page_ocr` (which then converts
    to page points) and :func:`image_ocr` (which keeps pixels).
    """
    fitz = D.fitz
    if preprocess_on:
        processed, transform = preprocess(rendered, src_dpi=src_dpi,
                                          target_dpi=target_dpi)
    else:
        processed, transform = rendered, Transform.identity()

    # Two verified quirks of MuPDF 1.28's pdfocr device: it silently
    # recognises NOTHING on a grayscale pixmap (RGB works), and the PNG's dpi
    # metadata is Tesseract's resolution hint, so it must reflect the
    # effective dpi of the (possibly upscaled) processed bitmap.
    effective_dpi = max(1, round(src_dpi * processed.width
                                 / max(rendered.width, 1)))
    buffer = io.BytesIO()
    processed.convert("RGB").save(buffer, format="PNG",
                                  dpi=(effective_dpi, effective_dpi))
    try:
        ocr_pdf = fitz.Pixmap(buffer.getvalue()).pdfocr_tobytes(
            language=lang, tessdata=str(pack_dir))
    except (RuntimeError, ValueError) as exc:
        raise OcrPackError(
            f"OCR failed for language '{lang}' using the packs in\n"
            f"    {pack_dir}\nReason: {exc}"
        ) from exc

    words: list[ImageWord] = []
    ocr_doc = fitz.open(stream=ocr_pdf, filetype="pdf")
    try:
        opage = ocr_doc[0]
        # The OCR pdf's page rect follows the PNG's dpi metadata, not its
        # pixel count, so the point->pixel factor is measured, never assumed.
        px_per_pt_x = processed.width / opage.rect.width
        px_per_pt_y = processed.height / opage.rect.height
        raw_words = opage.get_text("words")
        del opage
    finally:
        ocr_doc.close()
    for x0, y0, x1, y1, text, block, line, *_ in raw_words:
        corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
        mapped = []
        for cx, cy in corners:
            mapped.append(transform.invert_point(cx * px_per_pt_x,
                                                 cy * px_per_pt_y))
        xs = [p[0] for p in mapped]
        ys = [p[1] for p in mapped]
        words.append(ImageWord((min(xs), min(ys), max(xs), max(ys)),
                               str(text), int(block), int(line)))
    return words


def _flatten_rgb(image: Image.Image) -> Image.Image:
    """RGB copy with any transparency composited onto white."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(base, rgba).convert("RGB")
    return image.convert("RGB")


def image_ocr(image: Image.Image, *, lang: str = "eng",
              variant: str = "fast", preprocess_on: bool = True,
              tessdata_dir: Path | str,
              dpi: Optional[Sequence[float]] = None) -> list[ImageWord]:
    """OCR a raster image; word boxes are returned in image pixels.

    ``dpi`` is the image's declared resolution (``image.info["dpi"]`` when
    omitted); :func:`image_ocr_dpi` decides the source and upscale target.
    The image is never modified: preprocessing works on a converted copy.
    """
    _require_fitz()
    pack_dir = _resolve_pack_dir(tessdata_dir, lang, variant)
    declared = dpi if dpi is not None else image.info.get("dpi")
    src_dpi, target_dpi = image_ocr_dpi(image.size, declared)
    return _ocr_bitmap(_flatten_rgb(image), src_dpi, lang=lang,
                       pack_dir=pack_dir, preprocess_on=preprocess_on,
                       target_dpi=target_dpi)


def searchable_pdf_from_image(image: Image.Image,
                              words: Sequence[ImageWord], *, dpi: float,
                              image_bytes: Optional[bytes] = None) -> bytes:
    """One-page PDF: the picture at its physical size plus invisible text.

    The page is ``pixels * 72 / dpi`` points; ``image_bytes`` (the original
    file, when it is a single-frame format MuPDF embeds directly) avoids a
    lossy or bloated re-encode, otherwise the frame is embedded as PNG. Words
    use the same ``render_mode=3`` overlay as :func:`make_searchable`, so
    the picture's pixels are untouched while search and selection work.
    """
    _require_fitz()
    fitz = D.fitz
    scale = 72.0 / max(float(dpi), 1.0)
    doc = fitz.open()
    try:
        page = doc.new_page(width=image.width * scale,
                            height=image.height * scale)
        if image_bytes is None:
            buffer = io.BytesIO()
            _flatten_rgb(image).save(buffer, format="PNG")
            image_bytes = buffer.getvalue()
        page.insert_image(page.rect, stream=image_bytes)
        for word in words:
            x0, y0, x1, y1 = word.rect_px
            _insert_invisible_word(
                page, OcrWord((x0 * scale, y0 * scale, x1 * scale, y1 * scale),
                              word.text))
        # A throwaway document: garbage collection here renumbers nothing
        # the caller still holds.
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Make searchable


@dataclass(frozen=True)
class OcrReport:
    """Outcome of one :func:`make_searchable` run."""

    pages_done: int
    pages_skipped: int
    words_added: int
    cancelled: bool = False


def _insert_invisible_word(page_proxy, word: OcrWord) -> bool:
    """Insert one word as invisible text on an already-loaded page proxy.

    Baseline sits 22 percent of the box height above the bottom edge; the
    fontsize starts at 85 percent of the box height and shrinks when the
    rendered width would overflow the box by more than 20 percent, so
    double-click selection and search highlights line up with the scan.
    """
    x0, y0, x1, y1 = word.rect_pts
    width, height = x1 - x0, y1 - y0
    text = word.text.strip()
    if not text or width <= 0 or height <= 0:
        return False
    fontsize = max(4.0, height * 0.85)
    try:
        length = D.fitz.get_text_length(text, fontname="helv",
                                        fontsize=fontsize)
        if length > width * 1.2 and length > 0:
            fontsize = max(2.0, fontsize * width / length)
        page_proxy.insert_text((x0, y1 - 0.22 * height), text,
                               fontsize=fontsize, fontname="helv",
                               render_mode=3)
    except Exception:
        return False               # an unencodable word must not abort a run
    return True


def make_searchable(session, pages: Optional[Sequence[int]] = None, *,
                    lang: str = "eng", variant: str = "fast",
                    preprocess_on: bool = True, tessdata_dir: Path | str,
                    dpi: int = 300,
                    progress_cb: Optional[Callable[[int, int], None]] = None,
                    cancel=None) -> OcrReport:
    """Overlay invisible OCR text on every page that lacks a text layer.

    ONE ``session.checkpoint('Make searchable')`` wraps the entire run, so a
    single Ctrl+Z reverts everything. Pages with at least
    :data:`NATIVE_WORD_SKIP` native words are skipped and never overlaid
    twice. ``cancel`` (an object with ``is_set()``) is honoured between
    pages: the checkpoint's exception path restores the document wholesale,
    leaving zero new history entries and zero mutations.
    """
    document = session.document
    total_pages = document.page_count
    targets = (list(range(total_pages)) if pages is None
               else sorted({int(p) for p in pages}))
    for index in targets:
        if index < 0 or index >= total_pages:
            raise ValueError(f"page index {index} out of range")

    # Split skip-vs-OCR up front, so a run that would touch nothing never
    # opens a checkpoint (an empty snapshot would still mark the doc dirty).
    to_ocr: list[int] = []
    skipped = 0
    for index in targets:
        with document._lock:
            proxy = document._page(index)
            native = len(proxy.get_text("words"))
            del proxy
        if native >= NATIVE_WORD_SKIP:
            skipped += 1
        else:
            to_ocr.append(index)
    if not to_ocr:
        return OcrReport(0, skipped, 0)

    done = added = 0
    try:
        with session.checkpoint("Make searchable"):
            for position, index in enumerate(to_ocr):
                if cancel is not None and cancel.is_set():
                    raise _OcrCancelled()
                words = page_ocr(document, index, lang=lang, variant=variant,
                                 preprocess_on=preprocess_on, dpi=dpi,
                                 tessdata_dir=tessdata_dir)
                with document._lock:
                    proxy = document._page(index)
                    for word in words:
                        if _insert_invisible_word(proxy, word):
                            added += 1
                    del proxy
                done += 1
                if progress_cb is not None:
                    progress_cb(position + 1, len(to_ocr))
    except _OcrCancelled:
        # checkpoint already restored the before-bytes and pushed nothing.
        return OcrReport(0, skipped, 0, cancelled=True)
    return OcrReport(done, skipped, added)


# ---------------------------------------------------------------------------
# Scanned-page detection and cached text extraction


def needs_ocr(document, page: int) -> bool:
    """Heuristic "this is a scan": almost no native words while images cover
    more than half the page. Image areas are summed individually (overlaps
    can double-count), which is exact enough for a hint chip."""
    _require_fitz()
    fitz = D.fitz
    with document._lock:
        proxy = document._page(page)
        native = len(proxy.get_text("words"))
        if native >= NATIVE_WORD_TEXTLESS:
            del proxy
            return False
        page_rect = proxy.rect
        area = float(page_rect.width * page_rect.height)
        covered = 0.0
        try:
            infos = proxy.get_image_info()
        except Exception:
            infos = []
        for info in infos:
            rect = fitz.Rect(info["bbox"])
            rect.intersect(page_rect)
            if not rect.is_empty:
                covered += float(rect.width * rect.height)
        del proxy
    return area > 0 and min(covered, area) / area > IMAGE_COVERAGE_MIN


def words_to_text(words: Sequence[OcrWord]) -> str:
    """Plain text from word boxes: grouped into lines by vertical overlap,
    each line ordered left to right."""
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (w.rect_pts[1], w.rect_pts[0]))
    lines: list[list[OcrWord]] = []
    for word in ordered:
        center_y = (word.rect_pts[1] + word.rect_pts[3]) / 2.0
        if lines:
            current = lines[-1]
            top = min(w.rect_pts[1] for w in current)
            bottom = max(w.rect_pts[3] for w in current)
            if top <= center_y <= bottom:
                current.append(word)
                continue
        lines.append([word])
    return "\n".join(
        " ".join(w.text for w in sorted(line, key=lambda w: w.rect_pts[0]))
        for line in lines
    )


def ocr_text(document, page: int, cache: dict, *, lang: str = "eng",
             variant: str = "fast", preprocess_on: bool = True,
             dpi: int = 300, tessdata_dir: Path | str) -> str:
    """Plain OCR text for a page, memoized in the caller-owned ``cache``
    keyed ``(page, document.content_version)`` so an edited page is re-read
    while an untouched one costs nothing. Stale versions of the same page
    are pruned as they are superseded."""
    key = (int(page), int(getattr(document, "content_version", 0)))
    words = cache.get(key)
    if words is None:
        words = page_ocr(document, page, lang=lang, variant=variant,
                         preprocess_on=preprocess_on, dpi=dpi,
                         tessdata_dir=tessdata_dir)
        for stale in [k for k in cache if k[0] == key[0] and k != key]:
            del cache[stale]
        cache[key] = words
    return words_to_text(words)


def make_text_provider(*, tessdata_dir: Path | str, lang: str = "eng",
                       variant: str = "fast", preprocess_on: bool = True,
                       dpi: int = 300, cache: Optional[dict] = None
                       ) -> Callable[[object, int], str]:
    """Bind OCR settings into a ``(document, page) -> str`` callable suitable
    for :attr:`EditSession.ocr_text_provider`, sharing one word cache."""
    store: dict = {} if cache is None else cache

    def provider(document, page: int) -> str:
        return ocr_text(document, page, store, lang=lang, variant=variant,
                        preprocess_on=preprocess_on, dpi=dpi,
                        tessdata_dir=tessdata_dir)

    return provider


# ---------------------------------------------------------------------------
# Benchmark metric


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate: Levenshtein distance over the reference length
    (two-row dynamic programming, pure Python). Exported for the v3.5
    benchmark's with/without-preprocessing comparison."""
    if reference == hypothesis:
        return 0.0
    previous = list(range(len(hypothesis) + 1))
    for row, ref_char in enumerate(reference, start=1):
        current = [row]
        for col, hyp_char in enumerate(hypothesis, start=1):
            cost = 0 if ref_char == hyp_char else 1
            current.append(min(previous[col] + 1,        # deletion
                               current[col - 1] + 1,     # insertion
                               previous[col - 1] + cost))  # substitution
        previous = current
    return previous[-1] / max(1, len(reference))
