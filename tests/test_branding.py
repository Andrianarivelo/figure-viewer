"""Regression checks for the public FigView identity."""
from __future__ import annotations

import unittest
from pathlib import Path

from figureviewer import APP_NAME, APP_VERSION


class BrandingTests(unittest.TestCase):
    """Keep the visible name and canonical launcher from drifting apart."""

    def test_public_identity_is_figview(self) -> None:
        self.assertEqual(APP_NAME, "FigView")
        self.assertEqual(APP_VERSION, "3.4")

    def test_canonical_and_compatibility_launchers_are_present(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.assertTrue((root / "FigView.bat").is_file())
        self.assertTrue((root / "Figure Viewer.bat").is_file())


if __name__ == "__main__":
    unittest.main()
