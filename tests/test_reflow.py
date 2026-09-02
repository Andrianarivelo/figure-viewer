"""Reflow engine tests: block model, style capture, commits, find & replace.

Everything runs headless against PDFs generated on the fly; no user file is
ever read or written. The non-negotiable assertions mirror the design rules:
the source file on disk never changes, an edit or replace-all is exactly ONE
undo entry, redaction never leaks outside the edited block, and images and
neighbouring blocks survive a commit pixel for pixel.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import unittest
from pathlib import Path

from figureviewer import docs as D
from figureviewer import pdfedit as E
from figureviewer import reflow as R
from tests.test_pdfedit import _render_digest

#: Where the fixture image lands on page 0, in PDF points (kept clear of
#: every text block so a commit must leave it untouched).
IMAGE_RECT = (400.0, 500.0, 490.0, 590.0)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _png_bytes() -> bytes:
    """A small deterministic RGB gradient, generated in memory."""
    from PIL import Image

    image = Image.new("RGB", (60, 60))
    image.putdata([((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256)
                   for y in range(60) for x in range(60)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _block_with(block_list: list, needle: str):
    """The first block whose text contains ``needle`` (assert-friendly)."""
    for block in block_list:
        if needle in block.text:
            return block
    return None


def _center(block) -> tuple[float, float]:
    x0, y0, x1, y1 = block.bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _crop_samples(session: E.EditSession, page: int,
                  rect_pts: tuple[float, float, float, float]) -> bytes:
    """Rendered pixels of one page region, for pixel-identity comparisons."""
    document = session.document
    with document._lock:
        pix = document._doc[page].get_pixmap(clip=D.fitz.Rect(*rect_pts),
                                             dpi=150)
        return bytes(pix.samples)


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for reflow tests")
class ReflowTests(unittest.TestCase):
    """Block model, commit path and find & replace against a built fixture.

    Fixture layout (built in :meth:`setUp`):

    * page 0: block A ("Alpha bravo...", one line), a separate neighbour
      block whose bbox starts exactly 1 pt below block A's bbox (offset
      horizontally, which is what keeps MuPDF from merging the two), two
      sentence blocks carrying "colour" / "Fig. 3" for find & replace, and
      a gradient image at :data:`IMAGE_RECT`;
    * page 1: normal text on a page carrying ``/Rotate 90``;
    * page 2: a glyph run drawn with ``rotate=90`` (vertical text);
    * page 3: image only, no text layer (a scan stand-in).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "reflow_fixture.pdf"
        png = _png_bytes()

        doc = D.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Alpha bravo charlie delta", fontsize=11)
        # Place the neighbour so its line bbox starts exactly 1 pt below
        # block A's bbox: measure block A's bottom and its ascender offset
        # (origin y minus bbox top) from the live extraction.
        raw = page.get_text("rawdict")
        blk = [b for b in raw["blocks"] if b["type"] == 0][0]
        line = blk["lines"][0]
        ascent = line["spans"][0]["origin"][1] - line["bbox"][1]
        page.insert_text((322, blk["bbox"][3] + 1.0 + ascent),
                         "November oscar papa", fontsize=11)
        page.insert_text((72, 300), "The colour of the sky.", fontsize=11)
        page.insert_text((72, 340), "Fig. 3 shows the colour map.", fontsize=11)
        page.insert_image(D.fitz.Rect(*IMAGE_RECT), stream=png)

        rotated = doc.new_page(width=595, height=842)
        rotated.insert_text((72, 300), "Rotated page text", fontsize=12)
        rotated.set_rotation(90)

        vertical = doc.new_page(width=595, height=842)
        vertical.insert_text((300, 400), "vertical run", fontsize=12, rotate=90)

        scan = doc.new_page(width=595, height=842)
        scan.insert_image(D.fitz.Rect(100, 100, 500, 600), stream=png)

        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)
        self.document = self.session.document

    def _assert_source_untouched(self) -> None:
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "the raw file on disk must never change")

    # -- block model --------------------------------------------------------
    def test_blocks_model_hit_testing_and_char_geometry(self) -> None:
        page_blocks = R.blocks(self.document, 0)
        block = _block_with(page_blocks, "Alpha bravo charlie delta")
        self.assertIsNotNone(block)
        self.assertTrue(block.horizontal)
        hit = R.hit_block(self.document, 0, _center(block))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.index, block.index)
        self.assertEqual(R.refusal_reason(), "")
        # Character geometry: the rect for "bravo" sits inside the block
        # and to the right of the rect for "Alpha".
        start = block.text.index("bravo")
        rects = block.rects_for_range(start, start + len("bravo"))
        self.assertEqual(len(rects), 1)
        x0, y0, x1, y1 = rects[0]
        self.assertLess(x0, x1)
        self.assertLess(y0, y1)
        self.assertGreaterEqual(x0, block.bbox[0] - 0.1)
        self.assertLessEqual(x1, block.bbox[2] + 0.1)
        self.assertGreater(x0, block.bbox[0] + 5.0,
                           "'bravo' cannot start at the block's left edge")
        # A miss on empty canvas is a plain miss, not a refusal.
        self.assertIsNone(R.hit_block(self.document, 0, (560.0, 820.0)))
        self.assertEqual(R.refusal_reason(), "")

    def test_fixture_neighbour_really_is_a_separate_block_one_point_away(
            self) -> None:
        page_blocks = R.blocks(self.document, 0)
        block_a = _block_with(page_blocks, "Alpha bravo")
        neighbour = _block_with(page_blocks, "November oscar papa")
        self.assertIsNotNone(block_a)
        self.assertIsNotNone(neighbour)
        self.assertNotEqual(block_a.index, neighbour.index)
        gap = neighbour.bbox[1] - block_a.bbox[3]
        self.assertAlmostEqual(gap, 1.0, delta=0.25,
                               msg="fixture must keep the 1 pt separation")

    def test_hit_block_refuses_rotated_glyphs_with_a_reason(self) -> None:
        block = _block_with(R.blocks(self.document, 2), "vertical run")
        self.assertIsNotNone(block)
        self.assertFalse(block.horizontal)
        self.assertIsNone(R.hit_block(self.document, 2, _center(block)))
        self.assertEqual(R.refusal_reason(), R.REFUSAL_ROTATED)
        with self.assertRaises(R.ReflowError):
            R.commit_edit(self.session, 2, block, "impossible")
        self.assertFalse(self.session.can_undo,
                         "a refused commit must leave no history entry")
        self._assert_source_untouched()

    # -- style capture ------------------------------------------------------
    def test_style_capture_pins_bold_italic_center_and_line_height(self) -> None:
        style_pdf = self.root / "style.pdf"
        doc = D.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Bold statement text", fontsize=11,
                         fontname="hebo")
        page.insert_text((72, 160), "Italic aside text", fontsize=11,
                         fontname="heit")
        page.insert_textbox(D.fitz.Rect(100, 300, 400, 420),
                            "Centered heading\n"
                            "with a much longer second line here\n"
                            "short tail",
                            fontsize=12, fontname="helv",
                            align=D.fitz.TEXT_ALIGN_CENTER)
        page.insert_text((72, 500), "Plain measured line one", fontsize=11)
        page.insert_text((72, 516), "Plain measured line two", fontsize=11)
        doc.save(str(style_pdf))
        doc.close()
        document = D.load_document(_entry(style_pdf))
        self.addCleanup(document.close)
        page_blocks = R.blocks(document, 0)

        bold = R.capture_style(_block_with(page_blocks, "Bold statement"))
        self.assertTrue(bold.bold)
        self.assertFalse(bold.italic)
        self.assertEqual(bold.source_font, "Helvetica-Bold")
        self.assertIn("Helvetica", bold.font_family_css)
        self.assertFalse(bold.font_fallback)
        self.assertEqual(bold.align, "left")
        self.assertAlmostEqual(bold.size, 11.0, delta=0.2)
        self.assertEqual(bold.color, "#000000")

        italic = R.capture_style(_block_with(page_blocks, "Italic aside"))
        self.assertTrue(italic.italic)
        self.assertFalse(italic.bold)

        centered = R.capture_style(_block_with(page_blocks, "Centered heading"))
        self.assertEqual(centered.align, "center")

        pitched = _block_with(page_blocks, "Plain measured line one")
        self.assertEqual(len(pitched.lines), 2,
                         "the two 16 pt-pitch lines must form one block")
        measured = R.capture_style(pitched)
        self.assertAlmostEqual(measured.line_height, 16.0 / 11.0, delta=0.05)

    def test_font_map_falls_back_and_records_it(self) -> None:
        family, fallback = R.css_family_for("ABCDEE+TimesNewRomanPSMT")
        self.assertIn("Times New Roman", family)
        self.assertFalse(fallback)
        family, fallback = R.css_family_for("BDFGHK+ArialMT")
        self.assertIn("Arial", family)
        self.assertFalse(fallback)
        family, fallback = R.css_family_for("ABCDEE+MysteryGroteskSans")
        self.assertEqual(family, "sans-serif")
        self.assertTrue(fallback, "an unmapped font must record the fallback")
        family, fallback = R.css_family_for("ABCDEE+OldStyleRoman")
        self.assertEqual(family, "serif")
        self.assertTrue(fallback)

    def test_merge_overrides_wins_over_builtins_and_ignores_junk(self) -> None:
        settings = {"reflow_font_overrides": {
            "Mystery Grotesk Sans": "Mystery, sans-serif",
            3: "not a string key",
            "bad value": 7,
            "   ": "blank key",
        }}
        table = R.merge_overrides(settings)
        self.assertEqual(len(table), len(R.FONT_CSS_MAP) + 1)
        family, fallback = R.css_family_for("ABCDEE+MysteryGroteskSans-Regular",
                                            table)
        self.assertEqual(family, "Mystery, sans-serif")
        self.assertFalse(fallback)
        # An override may also shadow a built-in row, because it sits on top.
        shadow = R.merge_overrides({"reflow_font_overrides":
                                    {"Times New Roman": "MyTimes, serif"}})
        self.assertEqual(R.css_family_for("ABCDEE+TimesNewRomanPSMT", shadow),
                         ("MyTimes, serif", False))
        self.assertEqual(R.merge_overrides({}), R.FONT_CSS_MAP)
        self.assertEqual(R.merge_overrides("not a dict"), R.FONT_CSS_MAP)

    # -- commit path --------------------------------------------------------
    def test_commit_edit_replaces_text_in_one_undo_entry(self) -> None:
        before = _render_digest(self.session, 0)
        block = R.hit_block(self.document, 0, _center(
            _block_with(R.blocks(self.document, 0), "Alpha bravo")))
        report = R.commit_edit(self.session, 0, block, "Zulu yankee xray")
        self.assertAlmostEqual(report.scale, 1.0, places=3,
                               msg="a short replacement must not shrink")
        self.assertGreaterEqual(report.grew_by, 0.0)
        text = self.session.page_text(0)
        self.assertIn("Zulu yankee xray", text)
        self.assertNotIn("Alpha", text)
        self.assertNotIn("bravo", text)
        self.assertEqual(len(self.session._history.undo), 1,
                         "one gesture, one undo entry")
        self.assertEqual(self.session.undo_label, "Edit text")
        self.session.undo()
        self.assertEqual(_render_digest(self.session, 0), before)
        self.assertIn("Alpha bravo charlie delta", self.session.page_text(0))
        self._assert_source_untouched()

    def test_commit_preserves_the_image_and_the_block_one_point_away(
            self) -> None:
        crop_before = _crop_samples(self.session, 0, IMAGE_RECT)
        block = _block_with(R.blocks(self.document, 0), "Alpha bravo")
        R.commit_edit(self.session, 0, block, "Whiskey tango")
        text = self.session.page_text(0)
        self.assertIn("Whiskey tango", text)
        self.assertNotIn("Alpha", text)
        self.assertIn("November oscar papa", text,
                      "the neighbouring block 1 pt away must survive")
        crop_after = _crop_samples(self.session, 0, IMAGE_RECT)
        self.assertEqual(crop_after, crop_before,
                         "pixels of an image outside the block must not change")
        self._assert_source_untouched()

    def test_commit_on_a_rotate_90_page_lands_in_place(self) -> None:
        block = _block_with(R.blocks(self.document, 1), "Rotated page text")
        self.assertIsNotNone(block)
        self.assertTrue(block.horizontal,
                        "text on a /Rotate page reads as horizontal")
        hit = R.hit_block(self.document, 1, _center(block))
        self.assertIsNotNone(hit, "a /Rotate page block must stay editable")
        R.commit_edit(self.session, 1, hit, "Landed")
        text = self.session.page_text(1)
        self.assertIn("Landed", text)
        self.assertNotIn("Rotated page", text)
        landed = _block_with(R.blocks(self.document, 1), "Landed")
        self.assertIsNotNone(landed)
        old_rect = D.fitz.Rect(*block.bbox)
        new_rect = D.fitz.Rect(*landed.bbox)
        self.assertTrue(new_rect.intersects(old_rect),
                        "the replacement must land where the old text was")
        self._assert_source_untouched()

    # -- find ---------------------------------------------------------------
    def test_find_literal_case_and_whole_word(self) -> None:
        matches = R.find(self.document, "colour")
        self.assertEqual(len(matches), 2)
        for match in matches:
            self.assertEqual(match.page, 0)
            self.assertTrue(match.rects_pts)
            for x0, y0, x1, y1 in match.rects_pts:
                self.assertLess(x0, x1)
                self.assertLess(y0, y1)
            self.assertIn("colour", match.context)
        self.assertEqual(len(R.find(self.document, "COLOUR")), 2)
        self.assertEqual(len(R.find(self.document, "COLOUR", case=True)), 0)
        self.assertEqual(len(R.find(self.document, "colour", whole_word=True)),
                         2)
        self.assertEqual(len(R.find(self.document, "olou", whole_word=True)), 0)
        self.assertEqual(R.find(self.document, ""), [])

    def test_find_regex_maps_offsets_through_char_boxes(self) -> None:
        matches = R.find(self.document, r"Fig\. (\d+)", regex=True)
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertIn("Fig. 3", match.context)
        block = R.blocks(self.document, 0)[match.block_index]
        self.assertEqual(block.text[match.start:match.end], "Fig. 3")
        bx0, by0, bx1, by1 = block.bbox
        for x0, y0, x1, y1 in match.rects_pts:
            self.assertGreaterEqual(x0, bx0 - 0.1)
            self.assertLessEqual(x1, bx1 + 0.1)
            self.assertGreaterEqual(y0, by0 - 0.1)
            self.assertLessEqual(y1, by1 + 0.1)

    # -- replace ------------------------------------------------------------
    def test_replace_all_literal_is_one_undo_entry_and_undo_restores(
            self) -> None:
        before = _render_digest(self.session, 0)
        ticks: list[tuple[int, int]] = []
        report = R.replace_all(self.session, "colour", "color",
                               progress=lambda done, total:
                               ticks.append((done, total)))
        self.assertEqual(report.occurrences, 2)
        self.assertEqual(report.blocks_changed, 2)
        self.assertFalse(report.cancelled)
        self.assertAlmostEqual(report.min_scale, 1.0, places=3)
        self.assertEqual(ticks, [(1, 2), (2, 2)])
        text = self.session.page_text(0)
        self.assertNotIn("colour", text)
        self.assertIn("color", text)
        self.assertEqual(len(self.session._history.undo), 1,
                         "replace-all must be exactly ONE undo entry")
        self.assertEqual(self.session.undo_label, "Replace 2 matches")
        self.session.undo()
        self.assertEqual(_render_digest(self.session, 0), before,
                         "undo must restore the pre-replace render")
        self.assertIn("colour", self.session.page_text(0))
        self.assertFalse(self.session.can_undo)
        self._assert_source_untouched()

    def test_replace_all_regex_capture_group(self) -> None:
        report = R.replace_all(self.session, r"Fig\. (\d+)", r"Figure \1",
                               regex=True)
        self.assertEqual(report.occurrences, 1)
        self.assertEqual(report.blocks_changed, 1)
        text = self.session.page_text(0)
        self.assertIn("Figure 3", text)
        self.assertNotIn("Fig. 3", text)
        self.assertEqual(len(self.session._history.undo), 1)
        self._assert_source_untouched()

    def test_replace_all_skips_scan_pages(self) -> None:
        self.assertTrue(R.is_ocr_only(self.document, 3))
        self.assertFalse(R.is_ocr_only(self.document, 0))
        report = R.replace_all(self.session, "colour", "color", pages=[3])
        self.assertEqual((report.blocks_changed, report.occurrences), (0, 0))
        self.assertFalse(self.session.can_undo,
                         "a no-op replace must push no history entry")

    def test_cancelled_replace_changes_nothing(self) -> None:
        before = _render_digest(self.session, 0)
        cancel = threading.Event()
        cancel.set()
        report = R.replace_all(self.session, "colour", "color", cancel=cancel)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.blocks_changed, 0)
        self.assertEqual(_render_digest(self.session, 0), before)
        self.assertFalse(self.session.can_undo)
        self._assert_source_untouched()


if __name__ == "__main__":
    unittest.main()
