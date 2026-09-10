"""Outline (index) extraction and the docked index panel.

The headless part checks :meth:`Document.outline` against a generated PDF
with nested bookmarks, one of them on a page stored with /Rotate 90 so the
landing point must be pushed through the rotation matrix like word boxes.
The live part drives the real window: the panel appears for a PDF with an
outline, hides for one without, follows the current page, and navigates.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from PIL import Image

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import indexui


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _make_outlined_pdf(path: Path, *, outline: bool = True) -> None:
    import pymupdf

    doc = pymupdf.open()
    for number in range(4):
        page = doc.new_page(width=300, height=400)
        page.insert_text((30, 60), f"Page {number + 1}", fontsize=20)
        page.insert_text((30, 300), f"Deep target {number + 1}", fontsize=14)
    doc[2].set_rotation(90)
    if outline:
        doc.set_toc([
            [1, "Introduction", 1],
            [2, "Scope", 1, {"kind": 1, "page": 0, "to": pymupdf.Point(0, 290)}],
            [1, "Methods", 2],
            [2, "Rotated section", 3, {"kind": 1, "page": 2,
                                       "to": pymupdf.Point(0, 290)}],
            [3, "Detail", 3],
            [1, "Results", 4],
        ])
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class OutlineExtractionTests(unittest.TestCase):
    def test_outline_levels_pages_and_rotated_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outlined.pdf"
            _make_outlined_pdf(path)
            document = D.VectorDocument(_entry(path), path.read_bytes())
            try:
                entries = document.outline()
                self.assertEqual([(e.level, e.title, e.page) for e in entries], [
                    (1, "Introduction", 0), (2, "Scope", 0), (1, "Methods", 1),
                    (2, "Rotated section", 2), (3, "Detail", 2),
                    (1, "Results", 3)])
                # A plain page-level bookmark lands at (or within the
                # default 36 pt margin of) the top.
                self.assertTrue(entries[0].y_pts is None or entries[0].y_pts <= 36.0)
                # An explicit target keeps its vertical position...
                self.assertAlmostEqual(entries[1].y_pts, 290.0, places=3)
                # ...and on the rotated page it is expressed in the rendered
                # frame: 290 pt down the unrotated 400 pt page becomes a
                # horizontal offset, so the vertical landing is the x
                # coordinate (0) mapped through the 90 degree rotation.
                rotated = entries[3]
                width, height = document.page_size_points(2)
                self.assertGreater(width, height, "page 3 renders landscape")
                self.assertIsNone(rotated.y_pts,
                                  "a target on the rotated top edge lands at the top")
            finally:
                document.close()

    def test_documents_without_bookmarks_report_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.pdf"
            _make_outlined_pdf(path, outline=False)
            document = D.VectorDocument(_entry(path), path.read_bytes())
            try:
                self.assertEqual(document.outline(), [])
            finally:
                document.close()
            image_path = Path(directory) / "figure.png"
            Image.new("RGB", (40, 30), "white").save(image_path)
            stat = image_path.stat()
            raster = D.RasterDocument(
                D.Entry(image_path, stat.st_size, stat.st_mtime, D.KIND_IMAGE),
                image_path.read_bytes())
            try:
                self.assertEqual(raster.outline(), [])
            finally:
                raster.close()


class CurrentEntryTests(unittest.TestCase):
    def test_deepest_bookmark_at_or_before_page_is_current(self) -> None:
        controller = indexui.IndexController.__new__(indexui.IndexController)
        controller.entries = [
            D.OutlineEntry(1, "Intro", 0), D.OutlineEntry(2, "Scope", 0),
            D.OutlineEntry(1, "Methods", 3), D.OutlineEntry(1, "Broken", -1),
            D.OutlineEntry(1, "Results", 6),
        ]
        current = controller.current_entry_index
        self.assertEqual(current(0), 1)      # last bookmark starting on page 0
        self.assertEqual(current(2), 1)      # nothing starts on 1-2: stay
        self.assertEqual(current(3), 2)
        self.assertEqual(current(9), 4)
        controller.entries = [D.OutlineEntry(1, "Late", 5)]
        self.assertIsNone(current(2))        # before the first bookmark


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class IndexPanelLiveTests(unittest.TestCase):
    def _start(self, stack: ExitStack, root: Path, target: Path):
        settings_dir = root / "settings"
        stack.enter_context(mock.patch.object(app_module, "SETTINGS_DIR", settings_dir))
        stack.enter_context(mock.patch.object(
            app_module, "SETTINGS_PATH", settings_dir / "settings.json"))
        stack.enter_context(mock.patch.object(
            app_module, "INSTANCE_PATH", settings_dir / "instance.json"))
        stack.enter_context(mock.patch.dict(app_module.DEFAULT_SETTINGS, {
            "single_instance": False, "thumb_size": 0,
            "geometry": "1100x720+40+40",
        }))
        try:
            return app_module.App(root, target)
        except Exception as exc:
            self.skipTest(f"Tk cannot open a live window here: {exc}")

    @staticmethod
    def _wait(application, predicate, seconds: float = 6.0) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline and not predicate():
            application.root.update()
            time.sleep(0.005)

    def test_panel_shows_follows_and_navigates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outlined = root / "outlined.pdf"
            plain = root / "plain.pdf"
            _make_outlined_pdf(outlined)
            _make_outlined_pdf(plain, outline=False)
            with ExitStack() as stack:
                application = self._start(stack, root, outlined)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    self._wait(application, lambda: window.index.available)
                    index = window.index
                    application.root.update()
                    self.assertTrue(index.available)
                    self.assertTrue(window.index_panel.winfo_ismapped())
                    self.assertEqual(len(index.tree.get_children("")), 3)
                    self.assertEqual(index.count_var.get(), "6")

                    # Following: page 2 (index 1) is covered by "Methods".
                    window.change_pdf_page(1)
                    application.root.update()
                    self.assertEqual(index._current_iid, "o2")
                    self.assertIn("current", index.tree.item("o2", "tags"))

                    # Navigation lands on the bookmark's page and target.
                    self.assertTrue(index.navigate(index.entries[1]))   # Scope, y 290
                    self.assertEqual(window.page, 0)
                    self.assertGreater(window.off_y, 0.0)
                    self.assertTrue(index.navigate(index.entries[5]))   # Results
                    self.assertEqual(window.page, 3)
                    self.assertEqual(index._current_iid, "o5")

                    # Filtering flattens to matches only.
                    index.filter_var.set("detail")
                    application.root.update()
                    self.assertEqual(index.tree.get_children(""), ("o4",))
                    index.filter_var.set("")

                    # Toggle hides it; a document without bookmarks hides it too.
                    index.toggle()
                    application.root.update()
                    self.assertFalse(window.index_panel.winfo_ismapped())
                    index.toggle()
                    window.add_files([plain])
                    self._wait(application,
                               lambda: window.document is not None
                               and window.current is not None
                               and window.current.path == plain)
                    self._wait(application, lambda: not index.available)
                    application.root.update()
                    self.assertFalse(index.available)
                    self.assertFalse(window.index_panel.winfo_ismapped())
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
