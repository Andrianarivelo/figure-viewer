"""Functional-control icon rendering tests that do not require Tk."""
from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from figureviewer.icons import _draw_icon


class IconTests(unittest.TestCase):
    def test_every_ui_icon_has_visible_alpha_and_the_requested_size(self) -> None:
        names = (
            "previous", "next", "back5", "forward5", "play", "pause", "stop",
            "reload", "auto", "loop", "folder", "up", "zoom_in", "zoom_out",
            "fit", "actual", "rotate_left", "rotate_right", "snip", "files",
            "info", "copy", "path", "external", "video",
        )
        for name in names:
            with self.subTest(name=name):
                image = _draw_icon(name, 18, "#e8ecf4", "#5b8def")
                self.assertEqual(image.size, (18, 18))
                self.assertEqual(image.mode, "RGBA")
                self.assertIsNotNone(image.getchannel("A").getbbox())

    def test_generated_brand_assets_have_real_alpha_and_windows_sizes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with Image.open(root / "assets" / "figure-viewer-mark.png") as master:
            self.assertEqual(master.mode, "RGBA")
            self.assertEqual(master.getpixel((0, 0))[3], 0)
            self.assertEqual(master.getchannel("A").getextrema(), (0, 255))
        with Image.open(root / "assets" / "figure-viewer-mark-256.png") as runtime:
            self.assertEqual(runtime.size, (256, 256))
            self.assertEqual(runtime.mode, "RGBA")
        with Image.open(root / "figure-viewer.ico") as icon:
            self.assertEqual(
                icon.info.get("sizes"),
                {(16, 16), (24, 24), (32, 32), (48, 48),
                 (64, 64), (128, 128), (256, 256)},
            )


if __name__ == "__main__":
    unittest.main()
