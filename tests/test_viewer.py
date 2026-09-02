"""Pure viewer interaction and PDF reading-layout tests."""
from __future__ import annotations

import unittest

from figureviewer import viewer


class PdfReadingScaleTests(unittest.TestCase):
    def test_a4_defaults_to_reader_style_125_percent(self) -> None:
        base_dpi = 150.0
        a4_width_px = round(viewer.PDF_A4_WIDTH_POINTS * base_dpi / 72.0)
        scale = viewer.pdf_reading_scale(a4_width_px, base_dpi, canvas_width=1200)
        displayed_zoom = scale * base_dpi / viewer.PDF_SCREEN_DPI
        self.assertAlmostEqual(displayed_zoom, 1.25, places=2)

    def test_wider_page_is_reduced_to_the_same_reading_width(self) -> None:
        base_dpi = 150.0
        a4_width = round(viewer.PDF_A4_WIDTH_POINTS * base_dpi / 72.0)
        a3_width = round(841.89 * base_dpi / 72.0)
        a4_scale = viewer.pdf_reading_scale(a4_width, base_dpi, canvas_width=1600)
        a3_scale = viewer.pdf_reading_scale(a3_width, base_dpi, canvas_width=1600)
        self.assertAlmostEqual(a4_width * a4_scale, a3_width * a3_scale, delta=1.0)
        self.assertLess(a3_scale, a4_scale)

    def test_narrow_viewport_caps_horizontal_overflow(self) -> None:
        scale = viewer.pdf_reading_scale(1240, 150.0, canvas_width=500)
        self.assertLessEqual(1240 * scale, (500 - 24) * viewer.PDF_MAX_VIEWPORT_WIDTH)


if __name__ == "__main__":
    unittest.main()
