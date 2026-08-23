"""Folder scanning and media classification tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from figureviewer import docs as D


class MediaClassificationTests(unittest.TestCase):
    def test_video_suffixes_have_their_own_kind(self) -> None:
        for suffix in (".mp4", ".MOV", ".mkv", ".webm", ".m2ts"):
            self.assertEqual(D.suffix_kind(suffix), D.KIND_VIDEO)

    def test_filters_are_mutually_specific(self) -> None:
        self.assertEqual(D.kinds_for_filter(D.FILTER_IMAGES), {D.KIND_IMAGE})
        self.assertEqual(D.kinds_for_filter(D.FILTER_DOCS), {D.KIND_DOC})
        self.assertEqual(D.kinds_for_filter(D.FILTER_VIDEOS), {D.KIND_VIDEO})
        self.assertEqual(
            D.kinds_for_filter(D.FILTER_ALL),
            {D.KIND_IMAGE, D.KIND_DOC, D.KIND_VIDEO},
        )

    def test_folder_scan_includes_video_without_decoding_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "figure.png").write_bytes(b"not decoded by scan")
            (root / "movie.mp4").write_bytes(b"not decoded by scan")
            (root / "notes.txt").write_text("ignored", encoding="utf-8")

            entries, truncated = D.scan_folder(
                root, kinds=D.kinds_for_filter(D.FILTER_ALL), max_depth=0
            )

            self.assertFalse(truncated)
            self.assertEqual(
                [(entry.name, entry.kind) for entry in entries],
                [("figure.png", D.KIND_IMAGE), ("movie.mp4", D.KIND_VIDEO)],
            )


if __name__ == "__main__":
    unittest.main()
