"""Headless tests for the selectable text layer.

Selection semantics are pinned down exactly (word granular, reading order,
line-aware text and highlight output), and the source -> logical transform
is proven to be the inverse of the viewer's logical -> source transform for
every rotation and flip combination, since a mismatch would draw highlights
on the wrong words after a rotate.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from figureviewer import docs as D
from figureviewer import textlayer as T
from figureviewer import viewer


def _layer() -> T.TextLayer:
    """Three lines in two blocks::

        The quick brown        (block 0, line 0)   y 0..10
        fox jumps              (block 0, line 1)   y 20..30
        Second paragraph       (block 1, line 0)   y 50..60
    """
    raw = [
        (0, 0, 30, 10, "The", 0, 0, 0),
        (35, 0, 75, 10, "quick", 0, 0, 1),
        (80, 0, 125, 10, "brown", 0, 0, 2),
        (0, 20, 30, 30, "fox", 0, 1, 0),
        (35, 20, 80, 30, "jumps", 0, 1, 1),
        (0, 50, 60, 60, "Second", 1, 0, 0),
        (65, 50, 150, 60, "paragraph", 1, 0, 1),
    ]
    return T.TextLayer.from_words(raw)


class LayerStructureTests(unittest.TestCase):
    def test_lines_follow_block_and_line_indices(self) -> None:
        layer = _layer()
        self.assertEqual(len(layer), 7)
        self.assertEqual(layer.lines, [(0, 3), (3, 5), (5, 7)])
        self.assertEqual(layer.line_rects[0], (0, 0, 125, 10))
        self.assertEqual([layer.line_of(i) for i in range(7)],
                         [0, 0, 0, 1, 1, 2, 2])

    def test_empty_and_degenerate_words_are_dropped(self) -> None:
        layer = T.TextLayer.from_words([
            (0, 0, 10, 10, "   ", 0, 0, 0),      # whitespace only
            (0, 0, 0, 10, "zero-width", 0, 0, 1),
            (0, 0, 10, 10, "kept", 0, 0, 2),
        ])
        self.assertEqual([w.text for w in layer.words], ["kept"])

    def test_lines_are_inferred_when_no_indices_exist(self) -> None:
        # Words given out of order and without block/line: vertical overlap
        # groups them into two lines, each ordered left to right.
        words = [
            T.Word((40, 21, 60, 31), "world", -1, -1),
            T.Word((0, 0, 30, 10), "Hello", -1, -1),
            T.Word((0, 20, 35, 30), "brave", -1, -1),
            T.Word((35, 1, 70, 11), "there", -1, -1),
        ]
        layer = T.TextLayer(words)
        self.assertEqual(layer.text_for(layer.all_indices()),
                         "Hello there\nbrave world")


class HitAndCaretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layer = _layer()

    def test_hit_finds_the_word_under_the_pointer_with_slack(self) -> None:
        self.assertEqual(self.layer.hit(40, 5), 1)          # inside "quick"
        self.assertEqual(self.layer.hit(31, 5), 0)          # 1 px past "The"
        self.assertIsNone(self.layer.hit(32.5, 5, pad=0.0))  # gap, no slack
        self.assertIsNone(self.layer.hit(40, 40))           # between blocks

    def test_nearest_prefers_the_line_under_the_pointer(self) -> None:
        self.assertEqual(self.layer.nearest(200, 5), 2)     # past line end
        self.assertEqual(self.layer.nearest(-50, 25), 3)    # before line start
        self.assertEqual(self.layer.nearest(10, 38), 3)     # nearer line 1
        self.assertEqual(self.layer.nearest(10, 47), 5)     # nearer block 2

    def test_nearest_on_empty_layer_is_none(self) -> None:
        self.assertIsNone(T.TextLayer([]).nearest(0, 0))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layer = _layer()

    def test_drag_across_lines_selects_reading_order_range(self) -> None:
        anchor = self.layer.nearest(40, 5)     # "quick"
        focus = self.layer.nearest(10, 25)     # "fox"
        self.assertEqual(self.layer.range_indices(anchor, focus), [1, 2, 3])
        self.assertEqual(self.layer.text_for([1, 2, 3]), "quick brown\nfox")

    def test_reverse_drag_gives_the_same_range(self) -> None:
        self.assertEqual(self.layer.range_indices(3, 1),
                         self.layer.range_indices(1, 3))

    def test_dragging_past_the_end_of_a_line_takes_the_whole_line(self) -> None:
        focus = self.layer.nearest(500, 25)
        self.assertEqual(self.layer.range_indices(0, focus), [0, 1, 2, 3, 4])

    def test_rect_selection_picks_intersecting_words_only(self) -> None:
        # A column covering x 0..32 catches the first word of every line.
        self.assertEqual(self.layer.rect_indices((0, 0, 32, 60)), [0, 3, 5])
        self.assertEqual(self.layer.rect_indices((200, 200, 300, 300)), [])

    def test_line_and_all_helpers(self) -> None:
        self.assertEqual(self.layer.line_indices(4), [3, 4])
        self.assertEqual(self.layer.all_indices(), list(range(7)))

    def test_text_joins_words_with_spaces_and_lines_with_newlines(self) -> None:
        self.assertEqual(self.layer.text_for(self.layer.all_indices()),
                         "The quick brown\nfox jumps\nSecond paragraph")
        # Out-of-range and duplicate indices are ignored, order is by index.
        self.assertEqual(self.layer.text_for([6, 5, 5, 99, -1]),
                         "Second paragraph")

    def test_highlight_rects_are_one_ribbon_per_line(self) -> None:
        rects = self.layer.rects_for([1, 2, 3])
        self.assertEqual(rects, [(35, 0, 125, 10), (0, 20, 30, 30)])
        self.assertEqual(self.layer.rects_for([]), [])


class LineGeometryTests(unittest.TestCase):
    """Vertical writing and broken source grouping must not produce the
    full-page stripes seen on a rotated scan (2026-09-10 report)."""

    def test_vertical_line_gets_vertical_ribbon_and_caret(self) -> None:
        # One column of vertical text, top to bottom, then a second column.
        raw = [
            (50, 0, 68, 40, "Informations", 0, 0, 0),
            (50, 45, 68, 90, "Générales", 0, 0, 1),
            (50, 95, 68, 120, "1.", 0, 0, 2),
            (100, 0, 118, 30, "Durée", 1, 0, 0),
            (100, 35, 118, 70, "projet", 1, 0, 1),
        ]
        layer = T.TextLayer.from_words(raw)
        self.assertEqual(layer.line_vertical, [True, True])
        self.assertEqual(layer.rects_for([1, 2]), [(50, 45, 68, 120)])
        # Caret: a pointer beside the first column, at the height of the
        # second word, resolves to that word, not to the other column.
        self.assertEqual(layer.nearest(75, 60), 1)
        self.assertEqual(layer.nearest(109, 200), 4)   # past the column end

    def test_broken_source_lines_fall_back_to_geometry(self) -> None:
        # The source claims one "line" holding words scattered over the
        # page (fat in both directions); geometry rebuilds real lines.
        raw = [
            (0, 0, 30, 10, "The", 0, 0, 0),
            (0, 100, 30, 110, "fox", 0, 0, 1),
            (200, 0, 240, 10, "quick", 0, 0, 2),
            (200, 100, 245, 110, "jumps", 0, 0, 3),
        ]
        layer = T.TextLayer.from_words(raw)
        self.assertEqual(layer.lines, [(0, 2), (2, 4)])
        self.assertEqual(layer.text_for(layer.all_indices()),
                         "The quick\nfox jumps")
        self.assertEqual(layer.rects_for(layer.all_indices()),
                         [(0, 0, 240, 10), (0, 100, 245, 110)])

    def test_normal_lines_are_not_mistaken_for_broken(self) -> None:
        layer = _layer()
        self.assertEqual(layer.lines, [(0, 3), (3, 5), (5, 7)])
        self.assertEqual(layer.line_vertical, [False, False, False])


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class RotatedPageWordsTests(unittest.TestCase):
    """Document.words() must land on the rendered glyphs of a page whose
    /Rotate attribute is not zero."""

    def _dark_fraction(self, image, box) -> float:
        crop = image.convert("L").crop(tuple(int(round(v)) for v in box))
        pixels = list(crop.get_flattened_data()) if hasattr(
            crop, "get_flattened_data") else list(crop.getdata())
        return sum(1 for v in pixels if v < 128) / max(1, len(pixels))

    def test_words_follow_page_rotation(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rotated.pdf"
            doc = D.fitz.open()
            page = doc.new_page(width=300, height=200)
            page.insert_text((40, 100), "Rotated words", fontsize=28)
            page.set_rotation(90)
            doc.save(str(path))
            doc.close()
            stat = path.stat()
            entry = D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)
            document = D.VectorDocument(entry, path.read_bytes(), base_dpi=144)
            try:
                width, height = document.page_size(0)
                self.assertGreater(height, width, "rotated page is portrait")
                words = document.words(0)
                self.assertEqual([w[4] for w in words], ["Rotated", "words"])
                image = document.full_page(0)
                for x0, y0, x1, y1, *_ in words:
                    self.assertTrue(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)
                    self.assertGreater(self._dark_fraction(image, (x0, y0, x1, y1)),
                                       0.10, "box must cover its glyphs")
                # Rotated by 90 degrees, the line runs vertically on screen.
                x0, y0, x1, y1 = words[0][:4]
                self.assertGreater(y1 - y0, x1 - x0)
                layer = T.TextLayer.from_words(words)
                self.assertEqual(layer.line_vertical, [True])
            finally:
                document.close()


class TransformTests(unittest.TestCase):
    """source_box_to_logical must invert ViewerWindow._logical_box_to_source."""

    def _fake_window(self, rotation: int, flip_h: bool, flip_v: bool):
        document = SimpleNamespace(page_size=lambda page: (400, 300))
        return SimpleNamespace(document=document, page=0, rotation=rotation,
                               flip_h=flip_h, flip_v=flip_v)

    def test_round_trip_for_every_rotation_and_flip(self) -> None:
        source = (10.0, 20.0, 110.0, 50.0)
        for rotation in (0, 90, 180, 270):
            for flip_h in (False, True):
                for flip_v in (False, True):
                    window = self._fake_window(rotation, flip_h, flip_v)
                    logical = T.source_box_to_logical(
                        source, (400, 300), rotation, flip_h, flip_v)
                    back = viewer.ViewerWindow._logical_box_to_source(
                        window, logical)
                    self.assertEqual(
                        tuple(round(v, 6) for v in back), source,
                        f"rotation={rotation} flip_h={flip_h} flip_v={flip_v}")

    def test_rotated_box_stays_inside_the_rotated_page(self) -> None:
        x0, y0, x1, y1 = T.source_box_to_logical((0, 0, 400, 300),
                                                 (400, 300), 90)
        self.assertEqual((x0, y0, x1, y1), (0, 0, 300, 400))


if __name__ == "__main__":
    unittest.main()
