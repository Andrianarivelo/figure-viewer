"""Selectable text layer: word geometry, reading-order selection, copy text.

Coordinate frame
----------------
Every word rectangle lives in **source pixels**: the un-rotated, un-flipped
frame that :meth:`ViewerWindow._logical_box_to_source` produces. For a PDF
page that is ``points * base_dpi / 72``; for a raster image it is plain image
pixels. Keeping selections in that frame means rotating or flipping the view
never invalidates a selection: only the drawing transform changes, and
:func:`source_box_to_logical` (the exact inverse of the viewer's transform)
is the one place that mapping is written down.

Selection semantics
-------------------
Selection is word granular, in reading order, the way every desktop PDF
reader behaves: the *caret* for a pointer position is the nearest word on the
nearest line (:meth:`TextLayer.nearest`), and a drag selects every word
between the anchor caret and the focus caret (:meth:`TextLayer.range_indices`).
Dragging past the end of a line therefore selects the whole line, and
dragging upwards reverses the range. :meth:`TextLayer.rect_indices` is the
rectangular (column) alternative used with Ctrl+drag for tables.

Headless by design: no tkinter here, everything is covered by
``tests/test_textlayer.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

Rect = tuple[float, float, float, float]

#: Words whose vertical overlap with the current line is below this fraction
#: of the shorter height start a new line when no block/line indices exist.
LINE_OVERLAP_MIN = 0.5


@dataclass(frozen=True)
class Word:
    """One word in source pixels with its reading-order position."""

    rect: Rect
    text: str
    block: int = 0
    line: int = 0


def source_box_to_logical(box: Rect, page_size: tuple[float, float],
                          rotation: int, flip_h: bool = False,
                          flip_v: bool = False) -> Rect:
    """Forward transform: source page pixels -> logical (rotated, flipped).

    Exact inverse of ``ViewerWindow._logical_box_to_source``: rotation is
    applied first, then the flips, on the rotated page extent.
    """
    width, height = page_size
    x0, y0, x1, y1 = box
    rotation %= 360
    if rotation == 0:
        logical = (x0, y0, x1, y1)
    elif rotation == 90:
        logical = (height - y1, x0, height - y0, x1)
    elif rotation == 180:
        logical = (width - x1, height - y1, width - x0, height - y0)
    else:
        logical = (y0, width - x1, y1, width - x0)
    lw, lh = (height, width) if rotation % 180 else (width, height)
    x0, y0, x1, y1 = logical
    if flip_h:
        x0, x1 = lw - x1, lw - x0
    if flip_v:
        y0, y1 = lh - y1, lh - y0
    return (x0, y0, x1, y1)


def _distance_1d(lo: float, hi: float, value: float) -> float:
    """Distance from ``value`` to the closed interval ``[lo, hi]``."""
    if value < lo:
        return lo - value
    if value > hi:
        return value - hi
    return 0.0


def _union(rects: Sequence[Rect]) -> Rect:
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


class TextLayer:
    """Immutable set of words plus the line structure needed for selection.

    ``words`` keep their given order, which is the reading order the source
    reported (PyMuPDF blocks and lines, or Tesseract's line order). Lines are
    runs of consecutive words sharing ``(block, line)``; when a source gives
    no such indices (all ``< 0``) lines are inferred from vertical overlap.
    """

    def __init__(self, words: Iterable[Word]) -> None:
        cleaned = [w for w in words
                   if w.text.strip() and w.rect[2] > w.rect[0]
                   and w.rect[3] > w.rect[1]]
        if cleaned and all(w.block < 0 or w.line < 0 for w in cleaned):
            cleaned = self._infer_lines(cleaned)
        self.words: tuple[Word, ...] = tuple(cleaned)
        #: ``(start, end)`` word index ranges, one per line, in order.
        self.lines: list[tuple[int, int]] = []
        #: Union rectangle of each line's words, parallel to ``lines``.
        self.line_rects: list[Rect] = []
        self._line_of: list[int] = []
        start = 0
        for index in range(1, len(self.words) + 1):
            last = index == len(self.words)
            if last or ((self.words[index].block, self.words[index].line)
                        != (self.words[index - 1].block,
                            self.words[index - 1].line)):
                self.lines.append((start, index))
                self.line_rects.append(_union([w.rect for w in
                                               self.words[start:index]]))
                self._line_of.extend([len(self.lines) - 1] * (index - start))
                start = index

    # ---------------------------------------------------------- factories
    @classmethod
    def from_words(cls, raw: Iterable[Sequence]) -> "TextLayer":
        """Build from ``(x0, y0, x1, y1, text, block, line, ...)`` tuples in
        source pixels: the shape :meth:`Document.words` returns."""
        words = []
        for item in raw:
            x0, y0, x1, y1, text = item[:5]
            block = int(item[5]) if len(item) > 5 else 0
            line = int(item[6]) if len(item) > 6 else 0
            words.append(Word((float(x0), float(y0), float(x1), float(y1)),
                              str(text), block, line))
        return cls(words)

    @staticmethod
    def _infer_lines(words: list[Word]) -> list[Word]:
        """Assign line indices by vertical overlap, left to right per line."""
        ordered = sorted(words, key=lambda w: ((w.rect[1] + w.rect[3]) / 2.0,
                                               w.rect[0]))
        lines: list[list[Word]] = []
        for word in ordered:
            placed = False
            if lines:
                current = lines[-1]
                top = min(w.rect[1] for w in current)
                bottom = max(w.rect[3] for w in current)
                overlap = min(bottom, word.rect[3]) - max(top, word.rect[1])
                shorter = min(bottom - top, word.rect[3] - word.rect[1])
                if shorter > 0 and overlap / shorter >= LINE_OVERLAP_MIN:
                    current.append(word)
                    placed = True
            if not placed:
                lines.append([word])
        out: list[Word] = []
        for line_no, line in enumerate(lines):
            for word in sorted(line, key=lambda w: w.rect[0]):
                out.append(Word(word.rect, word.text, 0, line_no))
        return out

    # ---------------------------------------------------------- queries
    def __len__(self) -> int:
        return len(self.words)

    def line_of(self, index: int) -> int:
        return self._line_of[index]

    def hit(self, x: float, y: float, pad: float = 1.5) -> Optional[int]:
        """Index of the word under ``(x, y)`` (with ``pad`` slack), or None."""
        for line_no, (lx0, ly0, lx1, ly1) in enumerate(self.line_rects):
            if not (lx0 - pad <= x <= lx1 + pad and ly0 - pad <= y <= ly1 + pad):
                continue
            start, end = self.lines[line_no]
            for index in range(start, end):
                x0, y0, x1, y1 = self.words[index].rect
                if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                    return index
        return None

    def nearest(self, x: float, y: float) -> Optional[int]:
        """Caret for a pointer position: nearest word on the nearest line.

        Lines are ranked by vertical distance (zero when the point is inside
        the line's band), ties broken by horizontal distance so that two
        columns at the same height resolve to the one under the pointer.
        """
        if not self.words:
            return None
        best_line = None
        best_key = None
        for line_no, (lx0, ly0, lx1, ly1) in enumerate(self.line_rects):
            key = (_distance_1d(ly0, ly1, y), _distance_1d(lx0, lx1, x))
            if best_key is None or key < best_key:
                best_key, best_line = key, line_no
        start, end = self.lines[best_line]
        best_index = start
        best_dx = None
        for index in range(start, end):
            x0, _y0, x1, _y1 = self.words[index].rect
            dx = _distance_1d(x0, x1, x)
            if best_dx is None or dx < best_dx:
                best_dx, best_index = dx, index
        return best_index

    def range_indices(self, anchor: int, focus: int) -> list[int]:
        """Every word between two carets, inclusive, in reading order."""
        lo, hi = sorted((anchor, focus))
        lo = max(0, lo)
        hi = min(len(self.words) - 1, hi)
        return list(range(lo, hi + 1)) if lo <= hi else []

    def rect_indices(self, rect: Rect) -> list[int]:
        """Words whose box intersects ``rect`` (column selection)."""
        x0, y0, x1, y1 = rect
        out = []
        for index, word in enumerate(self.words):
            wx0, wy0, wx1, wy1 = word.rect
            if wx1 < x0 or wx0 > x1 or wy1 < y0 or wy0 > y1:
                continue
            out.append(index)
        return out

    def line_indices(self, index: int) -> list[int]:
        """All words on the line that contains word ``index``."""
        start, end = self.lines[self.line_of(index)]
        return list(range(start, end))

    def all_indices(self) -> list[int]:
        return list(range(len(self.words)))

    # ---------------------------------------------------------- output
    def text_for(self, indices: Sequence[int]) -> str:
        """Selected words joined by spaces, one line per source line."""
        chosen = sorted(set(i for i in indices if 0 <= i < len(self.words)))
        lines: list[str] = []
        current_line = None
        current: list[str] = []
        for index in chosen:
            line_no = self._line_of[index]
            if line_no != current_line and current:
                lines.append(" ".join(current))
                current = []
            current_line = line_no
            current.append(self.words[index].text)
        if current:
            lines.append(" ".join(current))
        return "\n".join(lines)

    def rects_for(self, indices: Sequence[int]) -> list[Rect]:
        """One highlight rectangle per line touched by the selection.

        Each rectangle spans the selected words horizontally and the full
        line band vertically, so a highlight looks like a continuous ribbon
        rather than a row of uneven word boxes.
        """
        chosen = sorted(set(i for i in indices if 0 <= i < len(self.words)))
        per_line: dict[int, list[Rect]] = {}
        for index in chosen:
            per_line.setdefault(self._line_of[index], []).append(
                self.words[index].rect)
        out = []
        for line_no in sorted(per_line):
            x0 = min(r[0] for r in per_line[line_no])
            x1 = max(r[2] for r in per_line[line_no])
            _lx0, ly0, _lx1, ly1 = self.line_rects[line_no]
            out.append((x0, ly0, x1, ly1))
        return out
