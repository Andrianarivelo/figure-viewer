"""In-place text editing engine tests (headless).

Everything runs on PDFs generated on the fly; no user file is read or
written. The assertions mirror the design rules of ``textedit``: preview and
commit share one layout, unchanged text never moves, backgrounds survive the
removal of old glyphs, a commit is exactly one undo entry, and the raw file
on disk never changes.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from figureviewer import docs as D
from figureviewer import reflow as R
from figureviewer import textedit as TE

if D.HAVE_FITZ:
    from figureviewer import pdfedit as E

CALIBRI = Path(r"C:\Windows\Fonts\calibri.ttf")
PANEL = (0.85, 0.92, 1.0)                    # light-blue background fill


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> "D.Entry":
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _spans(session, page: int = 0) -> list[tuple[str, tuple[float, float], str]]:
    """``(text, origin, font)`` of every span, NBSP folded to spaces."""
    with session.document._lock:
        raw = session.document._doc[page].get_text("dict")
    out = []
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                out.append((span["text"].replace("\xa0", " "),
                            (round(span["origin"][0], 2), round(span["origin"][1], 2)),
                            span["font"]))
    # Reading order (top to bottom, left to right), not content-stream order:
    # new text is appended at the end of the page's content stream.
    return sorted(out, key=lambda item: (item[1][1], item[1][0]))


def _pixel(session, page: int, point: tuple[float, float]) -> tuple[int, int, int]:
    with session.document._lock:
        pix = session.document._doc[page].get_pixmap(
            clip=D.fitz.Rect(point[0], point[1], point[0] + 1, point[1] + 1), dpi=72)
    return tuple(pix.pixel(0, 0)[:3])


class RichTextModelTests(unittest.TestCase):
    """Editing semantics, independent of PDF and Tk."""

    def setUp(self) -> None:
        self.base = TE.CharStyle(family="Helvetica", size=12)

    def test_typing_replaces_selection_and_coalesces_undo(self) -> None:
        model = TE.RichText("hello world", base_style=self.base)
        model.select_range(6, 11)
        for char in "there":
            model.insert(char)
        self.assertEqual(model.text, "hello there")
        self.assertEqual(model.caret, 11)
        # One undo step removes the whole typed word and restores the selection.
        self.assertTrue(model.undo())
        self.assertEqual(model.text, "hello world")
        self.assertEqual(model.selection(), (6, 11))
        self.assertTrue(model.redo())
        self.assertEqual(model.text, "hello there")

    def test_word_deletion_and_navigation(self) -> None:
        model = TE.RichText("alpha beta  gamma", base_style=self.base)
        model.set_caret(len(model.text))
        model.delete_backward(word=True)
        self.assertEqual(model.text, "alpha beta  ")
        self.assertEqual(model.word_left(len(model.text)), 6)
        self.assertEqual(model.word_right(0), 6)
        model.set_caret(0)
        model.delete_forward(word=True)
        self.assertEqual(model.text, "beta  ")

    def test_styles_follow_selection_or_whole_box(self) -> None:
        model = TE.RichText("make bold", base_style=self.base)
        model.select_range(5, 9)
        model.toggle_flag("bold")
        self.assertEqual([s.bold for s in model.styles], [False] * 5 + [True] * 4)
        # No selection: formatting applies to the whole box and to new text.
        model.set_caret(0)
        model.apply_style(size=20, color="#c00000")
        self.assertTrue(all(s.size == 20 for s in model.styles))
        self.assertEqual(model.base_style.color, "#c00000")
        model.set_caret(len(model.text))
        model.insert("!")
        self.assertTrue(model.styles[-1].bold, "typing continues the previous style")
        self.assertEqual(TE.clamp_size(10_000), TE.MAX_FONT_SIZE)

    def test_normalize_text_folds_nbsp_tabs_and_controls(self) -> None:
        self.assertEqual(TE.normalize_text("a\u00a0b\tc\r\nd\x07"), "a b    c\nd")


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required")
class LayoutTests(unittest.TestCase):
    """Real-metric layout: wrapping, alignment, caret geometry."""

    def setUp(self) -> None:
        self.registry = TE.FontRegistry(font_dirs=[])     # base-14 only
        self.style = TE.CharStyle(family="Helvetica", size=10)

    def _layout(self, text: str, **kwargs) -> TE.Layout:
        return TE.layout_rich(text, [self.style] * len(text), self.registry, **kwargs)

    def test_wrap_hard_breaks_and_widths(self) -> None:
        text = "one two three four five six seven\nlast"
        layout = self._layout(text, wrap_width=60.0)
        self.assertGreater(len(layout.lines), 2)
        for line in layout.lines:
            self.assertLessEqual(line.content_width, 60.0 + 1e-6)
        self.assertEqual(text[layout.lines[-1].start:layout.lines[-1].end], "last")
        self.assertTrue(layout.lines[-1].hard)
        # Auto width: one line per paragraph, box as wide as the longest.
        auto = self._layout(text)
        self.assertEqual(len(auto.lines), 2)
        self.assertAlmostEqual(auto.width, max(ln.content_width for ln in auto.lines))

    def test_alignment_offsets(self) -> None:
        text = "ab\nabcdef"
        center = self._layout(text, align="center")
        right = self._layout(text, align="right")
        short = center.lines[0]
        self.assertAlmostEqual(short.xs[0], (center.width - short.content_width) / 2)
        self.assertAlmostEqual(right.lines[0].xs[-1], right.width)

    def test_justify_fills_soft_lines_only(self) -> None:
        text = "aa bb cc dd ee ff gg hh ii jj kk ll"
        layout = self._layout(text, wrap_width=50.0, align="justify")
        for line in layout.lines[:-1]:
            stop = line.start + TE._content_len(text, line.start, line.end)
            self.assertAlmostEqual(line.xs[stop - line.start], 50.0, places=4)
        last = layout.lines[-1]
        self.assertLess(last.xs[-1], 50.0)
        # Justified words are drawn as separate runs at stretched positions.
        self.assertGreater(len(layout.runs()), len(layout.lines))

    def test_caret_and_hit_testing_roundtrip(self) -> None:
        text = "first line\nsecond"
        layout = self._layout(text, line_height=1.5)
        for index in range(len(text) + 1):
            x, baseline, ascent, _descent = layout.caret_geometry(index)
            self.assertEqual(layout.index_at(x + 0.01, baseline - ascent / 2), index)
        self.assertAlmostEqual(layout.lines[1].baseline, 15.0)
        rects = layout.selection_rects(6, 13)
        self.assertEqual(len(rects), 2, "a selection across a newline spans two lines")

    def test_fixed_baselines_and_indents(self) -> None:
        text = "a\nb\nc"
        layout = self._layout(text, fixed_baselines=[0.0, 14.0], indents=[0, 5, 0])
        self.assertEqual([ln.baseline for ln in layout.lines], [0.0, 14.0, 14.0 + 12.0])
        self.assertAlmostEqual(layout.lines[1].xs[0], 5.0)


class FontMatchingTests(unittest.TestCase):

    def setUp(self) -> None:
        self.registry = TE.FontRegistry(font_dirs=[])
        # A fake installed family index, independent of the machine.
        self.registry._families = {
            "Calibri": {(False, False): "calibri.ttf", (True, False): "calibrib.ttf"},
            "Times New Roman": {(False, False): "times.ttf"},
            "Arial": {(False, False): "arial.ttf"},
        }

    def test_embedded_names_map_to_installed_families(self) -> None:
        match = self.registry.match_pdf_font
        self.assertEqual(match("ABCDEF+Calibri-Bold"), ("Calibri", True, False, True))
        self.assertEqual(match("TimesNewRomanPS-ItalicMT")[:3],
                         ("Times New Roman", False, True))
        self.assertEqual(match("Calibri Regular", 0)[0], "Calibri")
        self.assertEqual(match("ArialMT")[0], "Arial")
        # Base-14 names stay on the base-14 font MuPDF drew them with.
        self.assertEqual(match("Helvetica-Bold")[:2], ("Helvetica", True))
        self.assertEqual(match("Courier")[0], "Courier")
        # Unknown fonts fall back honestly (exact = False).
        family, _b, _i, exact = match("XYZQQQ+MinionPro-Regular")
        self.assertEqual(family, "Times")
        self.assertFalse(exact)
        self.assertEqual(match("Foo", TE.FLAG_BOLD)[1], True)

    def test_family_names_and_face_fallback(self) -> None:
        self.assertEqual(TE.family_from_full_name("Calibri Bold Italic"), "Calibri")
        self.assertEqual(TE.family_from_full_name("Calibri Light"), "Calibri Light")
        face = self.registry.face("calibri", bold=True, italic=True)
        self.assertEqual(face.path, "calibrib.ttf", "nearest existing style wins")
        self.assertEqual(self.registry.face("No Such Font").base14, "helv")
        self.assertEqual(self.registry.face("Times", True, True).base14, "tibi")
        self.assertEqual(self.registry.families()[:3], ["Helvetica", "Times", "Courier"])


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required")
class CommitTests(unittest.TestCase):
    """Seeding from page text and committing through an EditSession."""

    PARA = ("Neurons in layer two showed a robust increase in firing rate after "
            "the stimulus onset and the effect persisted across sessions.")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf = Path(self._tmp.name) / "fixture.pdf"
        fitz = D.fitz
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(40, 40, 555, 400), color=None, fill=PANEL)
        page.insert_textbox(fitz.Rect(60, 60, 330, 200), self.PARA,
                            fontname="helv", fontsize=12, color=(0.2, 0.0, 0.5))
        page.insert_text((60, 300), "Figure 1: label", fontname="hebo", fontsize=14)
        rotated = doc.new_page(width=595, height=842)
        rotated.set_rotation(90)
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)
        self.registry = TE.FontRegistry(font_dirs=[])

    def _block(self, needle: str):
        for block in R.blocks(self.session.document, 0):
            if needle in block.text.replace("\xa0", " "):
                return block
        self.fail(f"no block containing {needle!r}")

    def _layout(self, seed: TE.BlockSeed, text: str = None, styles=None) -> TE.Layout:
        text = seed.text if text is None else text
        styles = seed.styles if styles is None else styles
        return TE.layout_rich(text, styles, self.registry, align=seed.align,
                              line_height=seed.line_height, wrap_width=seed.wrap_width,
                              fixed_baselines=seed.fixed_baselines, indents=seed.indents)

    def test_seed_recognises_paragraph_and_style(self) -> None:
        seed = TE.seed_from_block(self._block("Neurons"), self.registry)
        self.assertTrue(seed.paragraph)
        self.assertEqual(seed.styles[0].family, "Helvetica")
        self.assertEqual(seed.styles[0].color, "#330080")
        self.assertEqual(seed.inexact_fonts, [])
        self.assertNotIn("\xa0", seed.text)
        label = TE.seed_from_block(self._block("Figure 1"), self.registry)
        self.assertFalse(label.paragraph)
        self.assertTrue(label.styles[0].bold)

    def test_unchanged_commit_keeps_every_line_in_place(self) -> None:
        before = [s for s in _spans(self.session) if "Figure" not in s[0]]
        seed = TE.seed_from_block(self._block("Neurons"), self.registry)
        layout = self._layout(seed)
        self.assertEqual([seed.text[ln.start:ln.end].strip() for ln in layout.lines],
                         [s[0].strip() for s in before])
        TE.commit_text(self.session, 0, layout, seed.origin,
                       erase_rects=seed.erase_rects, label="Edit text")
        after = [s for s in _spans(self.session) if "Figure" not in s[0]]
        self.assertEqual([(t.strip(), o) for t, o, _f in after],
                         [(t.strip(), o) for t, o, _f in before])
        self.assertEqual(_sha(self.pdf), self.source_hash, "raw file untouched")

    def test_edit_keeps_first_baseline_background_and_is_one_undo(self) -> None:
        block = self._block("Neurons")
        seed = TE.seed_from_block(block, self.registry)
        first_origin = _spans(self.session)[0][1]
        undo_depth = len(self.session._history.undo)
        new_text = "Edited: short."  # leaves most of the old area empty
        layout = self._layout(seed, new_text, [seed.styles[0]] * len(new_text))
        TE.commit_text(self.session, 0, layout, seed.origin,
                       erase_rects=seed.erase_rects, label="Edit text")
        spans = _spans(self.session)
        self.assertTrue(spans[0][0].startswith("Edited:"))
        self.assertEqual(spans[0][1], first_origin, "first baseline must not move")
        self.assertEqual(len(self.session._history.undo), undo_depth + 1)
        # Between the old lines there must be panel colour, not a white box.
        x0, y0, x1, y1 = block.bbox
        probe = (x1 - 3.0, (y0 + y1) / 2.0)
        self.assertEqual(_pixel(self.session, 0, probe),
                         _pixel(self.session, 0, (520.0, 380.0)), "bare panel colour")
        self.session.undo()
        restored = _spans(self.session)
        self.assertTrue(restored[0][0].startswith("Neurons"))
        self.assertEqual(restored[0][1], first_origin)
        self.assertFalse(any(t.startswith("Edited") for t, _o, _f in restored))

    def test_new_text_and_delete_by_emptying(self) -> None:
        style = TE.CharStyle(family="Times", size=18, italic=True, color="#c00000")
        text = "New label"
        layout = TE.layout_rich(text, [style] * len(text), self.registry)
        TE.commit_text(self.session, 0, layout, (100.0, 500.0), label="Add text")
        added = [s for s in _spans(self.session) if s[0] == "New label"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0][1], (100.0, 500.0))
        self.assertIn("Italic", added[0][2])
        # Emptying an existing block deletes its glyphs and nothing else.
        seed = TE.seed_from_block(self._block("Figure 1"), self.registry)
        empty = TE.layout_rich("", [], self.registry)
        TE.commit_text(self.session, 0, empty, seed.origin,
                       erase_rects=seed.erase_rects, label="Edit text")
        texts = [t for t, _o, _f in _spans(self.session)]
        self.assertNotIn("Figure 1: label", texts)
        self.assertIn("New label", texts)

    def test_rotated_page_text_reads_upright(self) -> None:
        style = TE.CharStyle(family="Helvetica", size=12, underline=True)
        layout = TE.layout_rich("Upright", [style] * 7, self.registry)
        TE.commit_text(self.session, 1, layout, (80.0, 90.0))
        with self.session.document._lock:
            raw = self.session.document._doc[1].get_text("dict")
            drawings = self.session.document._doc[1].get_drawings()
        lines = [line for b in raw["blocks"] for line in b.get("lines", [])]
        self.assertEqual(len(lines), 1)
        self.assertEqual(tuple(round(v, 3) for v in lines[0]["dir"]), (1.0, 0.0))
        origin = lines[0]["spans"][0]["origin"]
        self.assertAlmostEqual(origin[0], 80.0, places=1)
        self.assertAlmostEqual(origin[1], 90.0, places=1)
        self.assertTrue(drawings, "underline drawn")

    def test_preview_matches_commit_geometry(self) -> None:
        style = TE.CharStyle(family="Helvetica", size=20)
        layout = TE.layout_rich("Hg", [style, style], self.registry)
        renderer = TE.PreviewRenderer()
        try:
            image, left, top = renderer.render(layout, 2.0)
        finally:
            renderer.close()
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0, "transparent background")
        self.assertLess(top, -10.0)
        self.assertAlmostEqual(image.width, round((layout.extent_x[1] - left + 2) * 2), delta=2)

    def test_erased_page_aligns_to_the_given_pixel_grid(self) -> None:
        block = self._block("Figure 1")
        seed = TE.seed_from_block(block, self.registry)
        erased = TE.ErasedPage(self.session.document, 0, seed.erase_rects)
        try:
            image, px, py = erased.render(block.bbox, 2.0, (-30.0, -40.0))
        finally:
            erased.close()
        # The pixmap's corner sits on the requested grid (within rounding).
        self.assertLess(abs(px - (block.bbox[0] * 2.0 - 30.0)), 1.0)
        self.assertLess(abs(py - (block.bbox[1] * 2.0 - 40.0)), 1.0)
        self.assertGreater(image.width, 10)
        # The label's glyphs are gone from the patch: it is panel colour only.
        colors = {c for _n, c in image.getcolors(maxcolors=100000)}
        self.assertLessEqual(len(colors), 3)
        # ...while the session document still has them.
        self.assertIn("Figure 1: label", [t for t, _o, _f in _spans(self.session)])

    @unittest.skipUnless(CALIBRI.is_file(), "Calibri is not installed")
    def test_system_font_embedding_is_subset_on_save(self) -> None:
        registry = TE.FontRegistry()
        style = TE.CharStyle(family="Calibri", size=12)
        layout = TE.layout_rich("Subset me", [style] * 9, registry)
        TE.commit_text(self.session, 0, layout, (60.0, 600.0))
        self.assertTrue(self.session.document.fonts_embedded)
        data = self.session.save_bytes()
        self.assertLess(len(data), 200_000, "a full Calibri would be ~800 KB")
        reopened = D.fitz.open(stream=data, filetype="pdf")
        try:
            self.assertIn("Subset me", reopened[0].get_text().replace("\xa0", " "))
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
