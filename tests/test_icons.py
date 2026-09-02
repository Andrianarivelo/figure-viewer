"""Functional-control icon rendering tests that do not require Tk.

This file deliberately never creates a Tk root, so the themed dialog classes
added alongside the edit glyphs (theme.ThemedDialog, theme.ThemedProgressDialog)
are exercised by the live-Tk GUI suite instead, not here.
"""
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

    def test_every_edit_glyph_renders_distinctly_and_not_as_the_fallback(self) -> None:
        """The PDF-editing glyph set must draw real shapes, not the circle.

        _draw_icon falls back to a plain circle for unknown names; a typo in a
        branch name would silently ship that circle. Render the fallback once
        from a name that can never exist and require every edit glyph to (a)
        differ from it pixel-for-pixel and (b) differ from every other glyph,
        so no two branches accidentally collapse into the same drawing.
        """
        names = (
            "pen", "highlighter", "underline", "strikeout", "squiggly",
            "textbox", "note", "ink", "shape", "arrow", "redact", "signature",
            "certificate", "form", "export", "ocr", "replace", "undo", "redo",
            "save", "pages", "select",
        )
        colour, accent = "#e8ecf4", "#5b8def"
        fallback = _draw_icon("definitely-not-a-glyph", 18, colour, accent).tobytes()
        seen: dict[bytes, str] = {}
        for name in names:
            with self.subTest(name=name):
                image = _draw_icon(name, 18, colour, accent)
                self.assertEqual(image.size, (18, 18))
                self.assertEqual(image.mode, "RGBA")
                self.assertIsNotNone(image.getchannel("A").getbbox())
                pixels = image.tobytes()
                self.assertNotEqual(pixels, fallback,
                                    f"glyph {name!r} rendered as the fallback circle")
                self.assertNotIn(pixels, seen,
                                 f"glyph {name!r} renders identically to {seen.get(pixels)!r}")
                seen[pixels] = name

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
