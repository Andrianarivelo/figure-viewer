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


class OptimalScaleTests(unittest.TestCase):
    """The 2026-09-10 report: a landscape A4 scan opened at 88 % in a 1400 px
    canvas under the reading rule; the wanted view fills the width."""

    def test_landscape_page_fills_the_canvas_width(self) -> None:
        base_dpi = 150.0
        landscape_px = round(841.89 * base_dpi / 72.0)          # 1754 px
        cap = viewer.PDF_OPTIMAL_MAX_ZOOM * viewer.PDF_SCREEN_DPI / base_dpi
        scale = viewer.optimal_scale(landscape_px, 1400, cap)
        self.assertAlmostEqual(landscape_px * scale, 1400 - 16, delta=1.0)
        reading = viewer.pdf_reading_scale(landscape_px, base_dpi, 1400)
        self.assertGreater(scale, reading * 1.3, "clearly larger than reading")

    def test_portrait_page_on_a_wide_screen_stops_at_the_cap(self) -> None:
        base_dpi = 150.0
        portrait_px = round(viewer.PDF_A4_WIDTH_POINTS * base_dpi / 72.0)
        cap = viewer.PDF_OPTIMAL_MAX_ZOOM * viewer.PDF_SCREEN_DPI / base_dpi
        scale = viewer.optimal_scale(portrait_px, 5000, cap)
        self.assertAlmostEqual(scale, cap)
        displayed = scale * base_dpi / viewer.PDF_SCREEN_DPI
        self.assertAlmostEqual(displayed, viewer.PDF_OPTIMAL_MAX_ZOOM)

    def test_small_image_is_not_blown_up_past_twice_its_pixels(self) -> None:
        scale = viewer.optimal_scale(300, 1400, viewer.IMAGE_OPTIMAL_MAX_SCALE)
        self.assertEqual(scale, viewer.IMAGE_OPTIMAL_MAX_SCALE)

    def test_bounds_and_degenerate_widths(self) -> None:
        self.assertGreaterEqual(viewer.optimal_scale(100000, 10, 2.0), viewer.ZOOM_MIN)
        self.assertLessEqual(viewer.optimal_scale(1, 100000, 100.0), viewer.ZOOM_MAX)
        self.assertGreater(viewer.optimal_scale(0, 800, 2.0), 0.0)


if __name__ == "__main__":
    unittest.main()
