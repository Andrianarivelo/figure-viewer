"""Folder scanning and media classification tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from figureviewer import docs as D


class MediaClassificationTests(unittest.TestCase):
    def test_pdf_page_format_detection_handles_orientation_and_custom_sizes(self) -> None:
        self.assertEqual(D.classify_pdf_page(595.28, 841.89), "A4 Portrait")
        self.assertEqual(D.classify_pdf_page(841.89, 595.28), "A4 Landscape")
        self.assertEqual(D.classify_pdf_page(612, 792), "Letter Portrait")
        self.assertEqual(D.classify_pdf_page(720, 720), "Custom 254 × 254 mm")

    def test_explicit_entries_preserve_order_and_never_scan_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.png"
            sibling = root / "not-opened.png"
            second = root / "second.pdf"
            for path in (first, sibling, second):
                path.write_bytes(b"metadata-only test")

            entries = D.entries_for_paths(
                [first, second, first],
                kinds={D.KIND_IMAGE, D.KIND_DOC, D.KIND_VIDEO},
            )

            self.assertEqual(
                [entry.path for entry in entries], [first.resolve(), second.resolve()]
            )
            self.assertNotIn(sibling.resolve(), {entry.path for entry in entries})

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

    def test_all_levels_finds_supported_files_at_every_nested_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                root / "top.png",
                root / "run_1" / "clip.mp4",
                root / "run_1" / "figures" / "report.pdf",
                root / "run_1" / "figures" / "final" / "plot.TIFF",
            )
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"classification does not decode content")
            (root / "run_1" / "figures" / "ignore.txt").write_text(
                "unsupported", encoding="utf-8"
            )

            entries, truncated = D.scan_folder(
                root,
                kinds=D.kinds_for_filter(D.FILTER_ALL),
                max_depth=D.DEPTH_UNLIMITED,
            )

            self.assertFalse(truncated)
            self.assertEqual({entry.path for entry in entries}, set(paths))
            self.assertEqual({entry.depth for entry in entries}, {0, 1, 2, 3})

            limited, _ = D.scan_folder(
                root,
                kinds=D.kinds_for_filter(D.FILTER_ALL),
                max_depth=2,
            )
            self.assertNotIn(paths[-1], {entry.path for entry in limited})


if __name__ == "__main__":
    unittest.main()
