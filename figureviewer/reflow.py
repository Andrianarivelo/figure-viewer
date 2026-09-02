"""Text block model, reflow edit commits and find & replace (headless).

Design rules (enforced by tests in ``tests/test_reflow.py``)
------------------------------------------------------------
1. **No tkinter.** This module is fully testable headless; the viewer's edit
   controller adapts it to the canvas.
2. **The raw file is sacred.** Everything mutates an
   :class:`figureviewer.pdfedit.EditSession` document in memory; the source
   path is never written.
3. **One undo entry per user gesture.** :func:`commit_edit` wraps its whole
   redact-and-reinsert sequence in ``session.checkpoint('Edit text')``;
   :func:`replace_all` commits every touched block inside ONE
   ``session.checkpoint('Replace N matches')``. Raw page mutations run inside
   the checkpoint body; the session mutators (``mark_redaction`` and friends)
   are never used here because they would push their own history entries.
4. **Honesty over magic.** Blocks containing non-horizontal writing
   (``wmode != 0`` or a line direction other than ``(1, 0)``) are refused by
   :func:`hit_block`; the reason is exposed through :func:`refusal_reason` so
   the UI can toast it. Pages without a native text layer are reported by
   :func:`is_ocr_only` so replace can be refused on scans. When the font map
   falls back, :class:`BlockStyle` records it so the UI can announce the
   substitution.
5. **Page proxy binding.** A PyMuPDF annot/textpage is bound to the exact
   ``Page`` object it came from. Every mutation sequence below loads one page
   proxy under the document lock and keeps it referenced for the whole
   sequence; after ``apply_redactions`` a fresh proxy is loaded before any
   further use.

Verified against the installed PyMuPDF 1.28.2 (spike, step 0):

* ``page.insert_htmlbox(rect, html, css=..., scale_low=...)`` returns
  ``(spare_height, scale)``; on overflow with ``scale_low=1.0`` it returns
  ``(-1, 1.0)`` and writes NOTHING, so growing the rect and retrying on the
  live page is safe.
* On a ``/Rotate`` page, ``get_text`` bboxes, ``add_redact_annot`` and
  ``insert_htmlbox`` all share the rotated (displayed) coordinate space, and
  ordinary text keeps ``dir == (1, 0)``; only genuinely rotated glyph runs
  report a different direction.
* Span flags: bit 4 (16) is bold, bit 1 (2) is italic.
* ``rawdict`` span character concatenation equals the ``dict`` span text, so
  one ``rawdict`` extraction serves both the block model and the char-box
  offset mapping used by regex search.
"""
from __future__ import annotations

import html as html_module
import re
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Union

from . import docs as D

# ---------------------------------------------------------------------------
# Tunables (all in PDF points unless stated otherwise)

#: Vertical inset applied to each redacted line box so the ascenders and
#: descenders of neighbouring lines and blocks are never clipped away.
LINE_INSET_PTS = 0.4
#: Line edges within this distance of the block edge count as aligned.
ALIGN_TOLERANCE_PTS = 1.5
#: Both-edges alignment only reads as "justify" with at least this many lines.
JUSTIFY_MIN_LINES = 3
DEFAULT_LINE_HEIGHT = 1.15
MIN_LINE_HEIGHT = 1.0
MAX_LINE_HEIGHT = 2.0
#: Extra growth beyond the measured overflow deficit on every retry.
GROW_STEP_PTS = 2.0
#: The grown rect never reaches closer than this to the page bottom.
PAGE_BOTTOM_MARGIN_PTS = 6.0
#: Final fallback: allow shrinking the text to 85 % to make it fit.
SCALE_LOW_FLOOR = 0.85
#: Pages with fewer native words than this are treated as scans.
OCR_ONLY_WORD_THRESHOLD = 3
_MAX_GROW_RETRIES = 8

FLAG_ITALIC = 1 << 1
FLAG_BOLD = 1 << 4

#: Embedded subset fonts are named like ``ABCDEE+TimesNewRomanPSMT``.
SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")
BOLD_NAME_TOKENS = ("bold", "black", "heavy")
ITALIC_NAME_TOKENS = ("italic", "oblique")

REFUSAL_ROTATED = "Rotated or vertical text cannot be edited"

#: Ordered substring table mapping normalised embedded font names to CSS
#: families with real fallback stacks. Checked top to bottom after stripping
#: the subset prefix, lowercasing, and removing spaces and hyphens; the first
#: matching substring wins, so the more specific names come first.
FONT_CSS_MAP: tuple[tuple[str, str], ...] = (
    ("timesnewroman", '"Times New Roman", Times, Georgia, serif'),
    ("times", '"Times New Roman", Times, Georgia, serif'),
    ("georgia", 'Georgia, "Times New Roman", serif'),
    ("garamond", 'Garamond, "Times New Roman", serif'),
    ("cambria", "Cambria, Georgia, serif"),
    ("helvetica", "Helvetica, Arial, sans-serif"),
    ("arial", "Arial, Helvetica, sans-serif"),
    ("calibri", "Calibri, Arial, sans-serif"),
    ("segoe", '"Segoe UI", Arial, sans-serif'),
    ("verdana", "Verdana, Geneva, sans-serif"),
    ("tahoma", "Tahoma, Geneva, sans-serif"),
    ("consolas", 'Consolas, "Courier New", monospace'),
    ("courier", '"Courier New", Courier, monospace'),
    ("mono", '"Courier New", Courier, monospace'),
)


class ReflowError(RuntimeError):
    """Raised when a reflow commit is impossible (e.g. rotated text)."""


class _ReplaceCancelled(Exception):
    """Internal: aborts a replace_all checkpoint so it restores cleanly."""


# ---------------------------------------------------------------------------
# Block model


@dataclass(frozen=True)
class SpanInfo:
    """One uniformly-styled glyph run, with per-character boxes."""

    bbox: tuple[float, float, float, float]
    text: str
    font: str
    flags: int
    size: float
    color: int
    origin: tuple[float, float]
    #: ``(character, bbox)`` pairs aligned with :attr:`text`.
    chars: tuple[tuple[str, tuple[float, float, float, float]], ...]


@dataclass(frozen=True)
class LineInfo:
    """One text line: geometry, writing direction and its spans."""

    bbox: tuple[float, float, float, float]
    wmode: int
    dir: tuple[float, float]
    spans: tuple[SpanInfo, ...]

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def baseline(self) -> float:
        """Baseline y of the line (first span origin, else the box bottom)."""
        return self.spans[0].origin[1] if self.spans else self.bbox[3]

    @property
    def horizontal(self) -> bool:
        dx, dy = self.dir
        return self.wmode == 0 and abs(dx - 1.0) <= 1e-3 and abs(dy) <= 1e-3


@dataclass(frozen=True)
class TextBlock:
    """One paragraph-level text block in PDF points (top-left origin)."""

    page: int
    index: int          # position among the page's text blocks
    bbox: tuple[float, float, float, float]
    lines: tuple[LineInfo, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def horizontal(self) -> bool:
        return all(line.horizontal for line in self.lines)

    def contains(self, point_pts: tuple[float, float]) -> bool:
        x, y = point_pts
        x0, y0, x1, y1 = self.bbox
        return x0 <= x <= x1 and y0 <= y <= y1

    def _char_entries(self) -> list[tuple[int, Optional[tuple[float, float, float, float]]]]:
        """Per-character ``(line_index, bbox)`` aligned with :attr:`text`.

        Newline separators between lines carry ``None`` boxes."""
        entries: list[tuple[int, Optional[tuple[float, float, float, float]]]] = []
        for line_index, line in enumerate(self.lines):
            if line_index:
                entries.append((line_index, None))
            for span in line.spans:
                for _char, box in span.chars:
                    entries.append((line_index, box))
        return entries

    def rects_for_range(self, start: int, end: int
                        ) -> tuple[tuple[float, float, float, float], ...]:
        """Bounding boxes of ``text[start:end]``, one rect per touched line."""
        entries = self._char_entries()
        per_line: dict[int, list[float]] = {}
        for line_index, box in entries[start:end]:
            if box is None:
                continue
            acc = per_line.get(line_index)
            if acc is None:
                per_line[line_index] = list(box)
            else:
                acc[0] = min(acc[0], box[0])
                acc[1] = min(acc[1], box[1])
                acc[2] = max(acc[2], box[2])
                acc[3] = max(acc[3], box[3])
        return tuple(tuple(per_line[key]) for key in sorted(per_line))


def blocks(document: D.VectorDocument, page: int) -> list[TextBlock]:
    """Extract the text-block model for one page.

    Built from ``rawdict`` rather than ``dict``: it is the same structured
    text tree plus per-character boxes, and the spike verified that the char
    concatenation equals the ``dict`` span text, so one extraction serves
    style capture, hit testing and offset-to-box mapping alike. The result is
    plain data detached from any page proxy.
    """
    with document._lock:
        raw = document._page(page).get_text("rawdict")
    result: list[TextBlock] = []
    for blk in raw.get("blocks", ()):
        if blk.get("type") != 0:
            continue
        lines: list[LineInfo] = []
        for ln in blk.get("lines", ()):
            spans: list[SpanInfo] = []
            for sp in ln.get("spans", ()):
                chars = tuple(
                    (str(c.get("c", "")), tuple(c.get("bbox", (0.0,) * 4)))
                    for c in sp.get("chars", ())
                )
                spans.append(SpanInfo(
                    bbox=tuple(sp.get("bbox", (0.0,) * 4)),
                    text="".join(c for c, _box in chars),
                    font=str(sp.get("font", "")),
                    flags=int(sp.get("flags", 0)),
                    size=float(sp.get("size", 0.0)),
                    color=int(sp.get("color", 0)),
                    origin=tuple(sp.get("origin", (0.0, 0.0))),
                    chars=chars,
                ))
            lines.append(LineInfo(
                bbox=tuple(ln.get("bbox", (0.0,) * 4)),
                wmode=int(ln.get("wmode", 0)),
                dir=tuple(ln.get("dir", (1.0, 0.0))),
                spans=tuple(spans),
            ))
        result.append(TextBlock(page=page, index=len(result),
                                bbox=tuple(blk.get("bbox", (0.0,) * 4)),
                                lines=tuple(lines)))
    return result


# The most recent hit_block refusal, exposed so the UI can toast honestly
# instead of guessing why a double-click did nothing.
_last_refusal = ""


def refusal_reason() -> str:
    """Why the last :func:`hit_block` returned ``None`` ('' = plain miss)."""
    return _last_refusal


def hit_block(document: D.VectorDocument, page: int,
              point_pts: tuple[float, float]) -> Optional[TextBlock]:
    """The editable text block under a page point, or ``None``.

    Blocks containing non-horizontal writing (vertical ``wmode`` or a line
    direction other than ``(1, 0)``, e.g. text inserted with ``rotate=90``)
    are refused: the redact-and-reinsert commit path can only produce
    horizontal HTML text, so pretending to edit them would corrupt layout.
    The refusal reason is available via :func:`refusal_reason`.
    """
    global _last_refusal
    _last_refusal = ""
    refused = False
    for block in blocks(document, page):
        if not block.contains(point_pts):
            continue
        if block.horizontal:
            return block
        refused = True
    if refused:
        _last_refusal = REFUSAL_ROTATED
    return None


# ---------------------------------------------------------------------------
# Style capture


@dataclass(frozen=True)
class BlockStyle:
    """Captured visual style of a block, expressed as CSS for reinsertion."""

    font_family_css: str
    size: float
    color: str                 # '#rrggbb'
    bold: bool
    italic: bool
    align: str                 # 'left' | 'center' | 'right' | 'justify'
    line_height: float
    source_font: str = ""      # embedded name, subset prefix stripped
    font_fallback: bool = False  # True when no FONT_CSS_MAP row matched

    def css(self) -> str:
        return (
            "div{margin:0;padding:0;"
            f"font-family:{self.font_family_css};"
            f"font-size:{self.size:.2f}pt;"
            f"color:{self.color};"
            f"text-align:{self.align};"
            f"line-height:{self.line_height:.2f};"
            "}"
        )


def normalized_font_name(font_name: str) -> str:
    """Lowercased font name, subset prefix, spaces and hyphens stripped."""
    bare = SUBSET_PREFIX_RE.sub("", font_name or "")
    return re.sub(r"[\s\-_,]+", "", bare).lower()


def merge_overrides(settings: dict) -> tuple[tuple[str, str], ...]:
    """The font table with ``settings['reflow_font_overrides']`` on top.

    Overrides are checked before the built-in rows, so a user mapping for an
    embedded base name always wins. Malformed entries in hand-edited JSON are
    ignored rather than crashing.
    """
    raw = settings.get("reflow_font_overrides") if isinstance(settings, dict) else {}
    rows: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for key, family in raw.items():
            if not isinstance(key, str) or not isinstance(family, str):
                continue
            token = normalized_font_name(key)
            if token and family.strip():
                rows.append((token, family.strip()))
    return tuple(rows) + FONT_CSS_MAP


def css_family_for(font_name: str,
                   table: Optional[Sequence[tuple[str, str]]] = None
                   ) -> tuple[str, bool]:
    """Map an embedded font name to a CSS family stack.

    Returns ``(family, fallback)``; ``fallback`` is ``True`` when no table
    row matched and only the serif-name heuristic decided, which is when the
    UI should toast the substitution.
    """
    rows = FONT_CSS_MAP if table is None else table
    name = normalized_font_name(font_name)
    for token, family in rows:
        if token and token in name:
            return family, False
    if "sans" in name:
        return "sans-serif", True
    if "serif" in name or "roman" in name:
        return "serif", True
    return "sans-serif", True


def _int_to_hex_color(value: int) -> str:
    return f"#{max(0, min(int(value), 0xFFFFFF)):06x}"


def _capture_alignment(block: TextBlock) -> str:
    lines = [line for line in block.lines if line.text.strip()]
    if not lines:
        return "left"
    bx0, _by0, bx1, _by1 = block.bbox
    block_center = (bx0 + bx1) / 2.0
    tol = ALIGN_TOLERANCE_PTS
    left = all(abs(line.bbox[0] - bx0) <= tol for line in lines)
    right = all(abs(bx1 - line.bbox[2]) <= tol for line in lines)
    center = all(abs((line.bbox[0] + line.bbox[2]) / 2.0 - block_center) <= tol
                 for line in lines)
    if left and right and len(lines) >= JUSTIFY_MIN_LINES:
        return "justify"
    if left:
        return "left"
    if center:
        return "center"
    if right:
        return "right"
    return "left"


def _capture_line_height(block: TextBlock, size: float) -> float:
    baselines = [line.baseline for line in block.lines if line.spans]
    pitches = [b1 - b0 for b0, b1 in zip(baselines, baselines[1:]) if b1 - b0 > 0.1]
    if not pitches or size <= 0:
        return DEFAULT_LINE_HEIGHT
    ratio = statistics.median(pitches) / size
    return max(MIN_LINE_HEIGHT, min(ratio, MAX_LINE_HEIGHT))


def capture_style(block: TextBlock,
                  table: Optional[Sequence[tuple[str, str]]] = None) -> BlockStyle:
    """Pin down the block's dominant style for faithful reinsertion.

    The dominant span is the one carrying the most characters; bold comes
    from flag bit 16 or a bold/black/heavy name substring (after stripping
    the subset prefix), italic from flag bit 2 or an italic/oblique name.
    """
    spans = [span for line in block.lines for span in line.spans
             if span.text.strip()]
    if not spans:
        spans = [span for line in block.lines for span in line.spans]
    if not spans:
        return BlockStyle(font_family_css="sans-serif", size=11.0,
                          color="#000000", bold=False, italic=False,
                          align="left", line_height=DEFAULT_LINE_HEIGHT,
                          source_font="", font_fallback=True)
    dominant = max(spans, key=lambda span: len(span.text))
    name = normalized_font_name(dominant.font)
    family, fallback = css_family_for(dominant.font, table)
    bold = bool(dominant.flags & FLAG_BOLD) or any(
        token in name for token in BOLD_NAME_TOKENS)
    italic = bool(dominant.flags & FLAG_ITALIC) or any(
        token in name for token in ITALIC_NAME_TOKENS)
    size = dominant.size if dominant.size > 0 else 11.0
    return BlockStyle(
        font_family_css=family,
        size=size,
        color=_int_to_hex_color(dominant.color),
        bold=bold,
        italic=italic,
        align=_capture_alignment(block),
        line_height=_capture_line_height(block, size),
        source_font=SUBSET_PREFIX_RE.sub("", dominant.font or ""),
        font_fallback=fallback,
    )


# ---------------------------------------------------------------------------
# Commit path: redact the old lines, reinsert reflowed HTML


@dataclass(frozen=True)
class CommitReport:
    """What a reflow commit actually did, for honest UI toasts."""

    scale: float          # 1.0 unless the text had to shrink to fit
    grew_by: float        # points added below the original block bbox
    font_fallback: bool
    source_font: str


def paragraph_html(text: str, style: BlockStyle) -> str:
    """Escaped paragraph HTML: blank-line split, ``<b>``/``<i>`` per style.

    Single newlines inside a paragraph become spaces on purpose: reflowing
    the paragraph to the (possibly grown) rect is the whole point.
    """
    paragraphs = [p for p in (part.strip() for part in re.split(r"\n\s*\n", text))
                  if p]
    if not paragraphs:
        paragraphs = [""]
    parts: list[str] = []
    for paragraph in paragraphs:
        body = html_module.escape(" ".join(paragraph.split()))
        if style.bold:
            body = f"<b>{body}</b>"
        if style.italic:
            body = f"<i>{body}</i>"
        parts.append(f"<div>{body}</div>")
    return "".join(parts)


def _required_height(width: float, html: str, css: str) -> float:
    """Height the HTML needs at scale 1.0 in a column of ``width`` points.

    Measured on a throwaway document with a very tall probe rect; the live
    page is never touched, so the measurement can never leave stray content.
    """
    fitz = D.fitz
    probe = fitz.open()
    try:
        tall = 4000.0
        page = probe.new_page(width=max(width, 1.0) + 20.0, height=tall)
        rect = fitz.Rect(10.0, 10.0, 10.0 + max(width, 1.0), tall - 10.0)
        spare, _scale = page.insert_htmlbox(rect, html, css=css, scale_low=1.0)
        if spare < 0:
            return rect.height * 2.0
        return rect.height - spare
    finally:
        probe.close()


def _replace_block_content(session, page: int, block: TextBlock, new_text: str,
                           style: BlockStyle) -> tuple[float, float]:
    """Raw redact-and-reinsert of one block. Returns ``(scale, grew_by)``.

    Runs inside a caller-owned ``session.checkpoint`` and therefore performs
    ONLY raw page mutations under the document lock: using the session's own
    ``mark_redaction``/``apply_redactions`` here would push extra history
    entries and break single-gesture undo.
    """
    fitz = D.fitz
    document = session.document
    html = paragraph_html(new_text, style)
    css = style.css()
    with document._lock:
        target = document._page(page)
        block_rect = fitz.Rect(*block.bbox)
        marked = 0
        for line in block.lines:
            rect = fitz.Rect(*line.bbox)
            # Inset vertically so ascenders/descenders of neighbouring lines
            # survive, then clamp into the block so an adjacent block a point
            # away is never touched.
            rect.y0 += LINE_INSET_PTS
            rect.y1 -= LINE_INSET_PTS
            rect.intersect(block_rect)
            if rect.is_empty or rect.is_infinite:
                continue
            target.add_redact_annot(rect)
            marked += 1
        if marked:
            # Exact flag usage from pdfedit.EditSession.apply_redactions:
            # images and vector art are preserved, only text is removed.
            target.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            # apply_redactions rebuilt the content stream: load a fresh page
            # proxy before inserting (proxy binding rule).
            target = document._page(page)
        rect = fitz.Rect(*block.bbox)
        page_bottom = target.rect.y1 - PAGE_BOTTOM_MARGIN_PTS
        spare, scale = target.insert_htmlbox(rect, html, css=css, scale_low=1.0)
        # Spike-verified: an overflowing insert with scale_low=1.0 writes
        # nothing and returns (-1, 1.0), so retrying on the live page is safe.
        retries = 0
        while spare < 0 and rect.y1 < page_bottom and retries < _MAX_GROW_RETRIES:
            deficit = _required_height(rect.width, html, css) - rect.height
            rect.y1 = min(rect.y1 + max(deficit, 0.0) + GROW_STEP_PTS, page_bottom)
            spare, scale = target.insert_htmlbox(rect, html, css=css,
                                                 scale_low=1.0)
            retries += 1
        if spare < 0:
            spare, scale = target.insert_htmlbox(rect, html, css=css,
                                                 scale_low=SCALE_LOW_FLOOR)
        if spare < 0:
            # Never lose the user's words: shrink whatever it takes and
            # report the true scale so the UI can say so.
            spare, scale = target.insert_htmlbox(rect, html, css=css,
                                                 scale_low=0)
        grew_by = max(0.0, rect.y1 - block.bbox[3])
    return float(scale), grew_by


def commit_edit(session, page: int, block: TextBlock, new_text: str,
                style: Optional[BlockStyle] = None) -> CommitReport:
    """Replace a block's text, reflowing it into a grown rect if needed.

    One ``session.checkpoint('Edit text')`` wraps the whole redaction and
    reinsertion, so a single Ctrl+Z reverts the entire edit. The returned
    :class:`CommitReport` carries the applied shrink scale (for the "text
    shrunk to N% to fit" toast), how far the rect grew, and whether the font
    map fell back.
    """
    if not block.horizontal:
        raise ReflowError(REFUSAL_ROTATED)
    if style is None:
        style = capture_style(block)
    with session.checkpoint("Edit text"):
        scale, grew_by = _replace_block_content(session, page, block,
                                                new_text, style)
    return CommitReport(scale=scale, grew_by=grew_by,
                        font_fallback=style.font_fallback,
                        source_font=style.source_font)


# ---------------------------------------------------------------------------
# Find & replace


@dataclass(frozen=True)
class Match:
    """One search hit, with page-point rects for highlight overlays."""

    page: int
    block_index: int      # index into blocks(document, page)
    rects_pts: tuple[tuple[float, float, float, float], ...]
    start: int            # offsets into the block's text
    end: int
    context: str


@dataclass(frozen=True)
class ReplaceReport:
    blocks_changed: int
    occurrences: int
    cancelled: bool = False
    #: Smallest shrink scale any block needed; 1.0 = nothing shrank.
    min_scale: float = 1.0


def compile_pattern(pattern: str, *, regex: bool = False, case: bool = False,
                    whole_word: bool = False) -> "re.Pattern[str]":
    """One compiled regex for both modes; ``re.error`` propagates to the UI."""
    source = pattern if regex else re.escape(pattern)
    if whole_word:
        source = r"\b(?:" + source + r")\b"
    return re.compile(source, 0 if case else re.IGNORECASE)


def _document_of(source) -> D.VectorDocument:
    """Accept an EditSession or a document; return the document."""
    return getattr(source, "document", source)


def _context(text: str, start: int, end: int, radius: int = 30) -> str:
    snippet = text[max(0, start - radius):min(len(text), end + radius)]
    return " ".join(snippet.split())


def is_ocr_only(document: D.VectorDocument, page: int) -> bool:
    """True for pages with (almost) no native text layer: scans.

    Search can still highlight OCR overlay results, but replace must be
    refused because there are no glyphs to redact; removing "text" from a
    scanned image would change nothing visible.
    """
    with document._lock:
        words = document._page(page).get_text("words")
    return len(words) < OCR_ONLY_WORD_THRESHOLD


def find(source, pattern: str, *, regex: bool = False, case: bool = False,
         whole_word: bool = False,
         pages: Optional[Iterable[int]] = None) -> list[Match]:
    """All matches of ``pattern``, mapped to blocks and page-point rects.

    Offsets are found with ``re`` over each block's text (literal patterns
    are escaped into the same engine) and mapped back to boxes through the
    rawdict per-character geometry. For literal single-token patterns,
    ``page.search_for`` is used as a fast page-level pre-filter so unmatched
    pages never pay for a rawdict extraction.
    """
    document = _document_of(source)
    if not pattern:
        return []
    compiled = compile_pattern(pattern, regex=regex, case=case,
                               whole_word=whole_word)
    # search_for is case-insensitive and substring-based: a safe superset
    # pre-filter, but only for single tokens (it treats line wraps unlike our
    # newline-joined block text when the pattern spans whitespace).
    use_prefilter = (not regex and bool(pattern.strip())
                     and not any(ch.isspace() for ch in pattern))
    matches: list[Match] = []
    page_range: Iterable[int] = (range(document.page_count) if pages is None
                                 else pages)
    for page in page_range:
        if use_prefilter:
            with document._lock:
                if not document._page(page).search_for(pattern):
                    continue
        for block in blocks(document, page):
            text = block.text
            for hit in compiled.finditer(text):
                if hit.start() == hit.end():
                    continue
                matches.append(Match(
                    page=page,
                    block_index=block.index,
                    rects_pts=block.rects_for_range(hit.start(), hit.end()),
                    start=hit.start(),
                    end=hit.end(),
                    context=_context(text, hit.start(), hit.end()),
                ))
    return matches


def replace_all(session, pattern: str, replacement: str, *,
                regex: bool = False, case: bool = False,
                whole_word: bool = False,
                pages: Optional[Iterable[int]] = None,
                progress: Optional[Callable[[int, int], None]] = None,
                cancel: Optional[object] = None) -> ReplaceReport:
    """Replace every match, committing all touched blocks as ONE undo entry.

    Matches are grouped per block and each block's full text is rewritten
    with ``re.sub`` (capture groups ``\\1`` / ``\\g<name>`` work in regex
    mode; literal replacements are inserted verbatim). Scanned pages
    (:func:`is_ocr_only`) and non-horizontal blocks are skipped: there is
    nothing honest to redact there. ``cancel`` (a ``threading.Event``-like
    object) aborts between blocks and the checkpoint restores the document,
    so a cancelled replace changes nothing at all.
    """
    document = session.document
    if not pattern:
        return ReplaceReport(0, 0)
    compiled = compile_pattern(pattern, regex=regex, case=case,
                               whole_word=whole_word)
    # re.sub interprets backslashes in the replacement; in literal mode the
    # user's replacement text must land verbatim.
    repl: Union[str, Callable] = (replacement if regex
                                  else lambda _m: replacement)

    # Plan every block edit up front, against the pre-edit geometry. Editing
    # one block never moves another block's glyphs, so the captured bboxes
    # stay valid for the whole pass inside one checkpoint.
    edits: list[tuple[int, TextBlock, str, int]] = []
    occurrences = 0
    page_range: Iterable[int] = (range(document.page_count) if pages is None
                                 else pages)
    for page in page_range:
        if is_ocr_only(document, page):
            continue
        for block in blocks(document, page):
            if not block.horizontal:
                continue
            new_text, count = compiled.subn(repl, block.text)
            if not count:
                continue
            occurrences += count
            if new_text != block.text:
                edits.append((page, block, new_text, count))
    if not edits:
        return ReplaceReport(0, occurrences)

    min_scale = 1.0
    done = 0
    try:
        with session.checkpoint(f"Replace {occurrences} matches"):
            for page, block, new_text, _count in edits:
                if cancel is not None and getattr(cancel, "is_set", bool)():
                    raise _ReplaceCancelled
                scale, _grew = _replace_block_content(
                    session, page, block, new_text, capture_style(block))
                min_scale = min(min_scale, scale)
                done += 1
                if progress is not None:
                    progress(done, len(edits))
    except _ReplaceCancelled:
        # The checkpoint already restored the pre-replace document and
        # pushed no history entry.
        return ReplaceReport(0, 0, cancelled=True)
    return ReplaceReport(blocks_changed=done, occurrences=occurrences,
                         min_scale=min_scale)
