"""Live Tk tests for text selection and image OCR in the viewer window.

These drive the real application the way test_editui.py does: an isolated
settings directory, deadline polling instead of sleeps, quit_all in a
finally. The clipboard is mocked so the machine's clipboard is never touched.
Image OCR runs only when the integrated engine and an ``eng`` pack exist.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import ocr as O
from figureviewer import textui

_ENG_DIR = O.find_language_dir(O.default_tessdata_root(), "eng")
OCR_READY = bool(D.HAVE_FITZ and _ENG_DIR is not None
                 and O.ocr_available(O.default_tessdata_root()))

LINES = ("The quick brown fox", "jumps over the lazy dog",
         "Neural data analysis 2026")


def _make_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=360, height=216)
    for row, line in enumerate(LINES):
        page.insert_text((36, 60 + 40 * row), line, fontsize=16)
    doc.new_page(width=360, height=216).insert_text((36, 60), "Second page",
                                                     fontsize=16)
    doc.save(str(path))
    doc.close()


def _make_image(path: Path, dpi: int = 200) -> None:
    """A PNG of the same three lines, rendered through PyMuPDF."""
    import pymupdf
    from PIL import Image

    doc = pymupdf.open()
    page = doc.new_page(width=360, height=216)
    for row, line in enumerate(LINES):
        page.insert_text((36, 60 + 40 * row), line, fontsize=16)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0),
                          alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), bytes(pix.samples))
    image.save(path, dpi=(dpi, dpi))
    doc.close()


class _LiveTest(unittest.TestCase):
    def _start(self, stack: ExitStack, root: Path, target: Path):
        settings_dir = root / "settings"
        stack.enter_context(mock.patch.object(app_module, "SETTINGS_DIR",
                                              settings_dir))
        stack.enter_context(mock.patch.object(
            app_module, "SETTINGS_PATH", settings_dir / "settings.json"))
        stack.enter_context(mock.patch.object(
            app_module, "INSTANCE_PATH", settings_dir / "instance.json"))
        stack.enter_context(mock.patch.dict(app_module.DEFAULT_SETTINGS, {
            "single_instance": False, "thumb_size": 0,
            "geometry": "1100x720+40+40",
        }))
        try:
            application = app_module.App(root, target)
        except Exception as exc:
            self.skipTest(f"Tk cannot open a live window here: {exc}")
        return application

    @staticmethod
    def _wait(application, predicate, seconds: float = 6.0) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline and not predicate():
            application.root.update()
            time.sleep(0.005)

    @staticmethod
    def _event(x: float, y: float, state: int = 0) -> SimpleNamespace:
        return SimpleNamespace(x=int(x), y=int(y), x_root=0, y_root=0,
                               state=state)

    @staticmethod
    def _word_center(controller, layer, text: str) -> tuple[float, float]:
        """Canvas coordinates of the middle of the first word ``text``."""
        index = next(i for i, w in enumerate(layer.words) if w.text == text)
        x0, y0, x1, y1 = controller._source_rect_to_canvas(layer.words[index].rect)
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class PdfTextSelectionTests(_LiveTest):
    def test_drag_select_copy_rotate_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "doc.pdf"
            _make_pdf(pdf_path)
            copied: list[str] = []
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    textui.winclip, "copy_text", copied.append))
                application = self._start(stack, root, pdf_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    self.assertIsNotNone(window.document)
                    controller = window.text
                    layer = controller.layer()
                    self.assertIsNotNone(layer)
                    self.assertEqual(len(layer), 13)

                    # --- hover: I-beam over a word, default elsewhere.
                    qx, qy = self._word_center(controller, layer, "quick")
                    window._on_motion(self._event(qx, qy))
                    self.assertEqual(str(window.canvas.cget("cursor")),
                                     textui.CURSOR_TEXT)
                    window._on_motion(self._event(2, 2))
                    self.assertEqual(str(window.canvas.cget("cursor")), "")

                    # --- press on blank page still pans (not consumed).
                    self.assertFalse(controller.on_press(self._event(2, 2)))

                    # --- drag from "quick" to "jumps" through the real
                    # viewer handlers: reading-order range, ribbon drawn.
                    jx, jy = self._word_center(controller, layer, "jumps")
                    window._on_press(self._event(qx, qy))
                    self.assertIsNone(window._pan_anchor)
                    window._on_drag(self._event(jx, jy))
                    window._on_release(self._event(jx, jy))
                    self.assertEqual(controller.selected_text(),
                                     "quick brown fox\njumps")
                    self.assertEqual(len(window.canvas.find_withtag("textsel")), 2)
                    self.assertIn("selected", window.status_var.get())

                    # --- Ctrl+C copies the text, not the image.
                    window._copy_shortcut()
                    self.assertEqual(copied, ["quick brown fox\njumps"])

                    # --- rotating keeps the selection (source-frame words).
                    window.rotate(90)
                    application.root.update()
                    self.assertEqual(controller.selected_text(),
                                     "quick brown fox\njumps")
                    self.assertEqual(len(window.canvas.find_withtag("textsel")), 2)
                    window.rotate(-90)

                    # --- double click selects a word, triple click a line.
                    self.assertTrue(controller.select_word_at(self._event(qx, qy)))
                    self.assertEqual(controller.selected_text(), "quick")
                    self.assertTrue(controller.select_line_at(self._event(qx, qy)))
                    self.assertEqual(controller.selected_text(), "The quick brown fox")

                    # --- Ctrl+drag: a column of first words.
                    tx, ty = self._word_center(controller, layer, "The")
                    nx, ny = self._word_center(controller, layer, "Neural")
                    x0, y0 = tx - 4, ty - 4
                    window._on_press(self._event(x0, y0, state=textui.STATE_CONTROL))
                    window._on_drag(self._event(nx + 4, ny + 4, state=textui.STATE_CONTROL))
                    window._on_release(self._event(nx + 4, ny + 4, state=textui.STATE_CONTROL))
                    self.assertEqual(controller.selected_text(),
                                     "The\njumps\nNeural")

                    # --- select all, Esc clears, plain click clears.
                    controller.select_all()
                    self.assertEqual(len(controller.selected), 13)
                    window._on_escape()
                    self.assertFalse(controller.has_selection())
                    self.assertEqual(window.canvas.find_withtag("textsel"), ())
                    controller.select_all()
                    window._on_press(self._event(qx, qy))
                    window._on_release(self._event(qx, qy))
                    self.assertFalse(controller.has_selection())

                    # --- page change drops the selection and reloads words.
                    controller.select_all()
                    window.change_pdf_page(1)
                    application.root.update()
                    self.assertFalse(controller.has_selection())
                    self.assertEqual(len(controller.layer()), 2)
                    self.assertEqual(controller.page_text().split(),
                                     ["Second", "page"])
                finally:
                    application.quit_all()


@unittest.skipUnless(OCR_READY, "integrated OCR engine or eng pack missing")
class ImageOcrTests(_LiveTest):
    def test_image_gains_a_selectable_layer_and_searchable_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "figure.png"
            _make_image(image_path)
            # The isolated settings directory owns the pack manager: seed it
            # with the real eng pack so no download is attempted.
            pack_dir = root / "settings" / "tessdata" / "fast"
            pack_dir.mkdir(parents=True)
            shutil.copyfile(_ENG_DIR / "eng.traineddata",
                            pack_dir / "eng.traineddata")
            copied: list[str] = []
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    textui.winclip, "copy_text", copied.append))
                application = self._start(stack, root, image_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    controller = window.text
                    self.assertIsNone(controller.layer())
                    self.assertFalse(controller.on_press(self._event(50, 50)))

                    # Run the OCR job the menu would start, bypassing the
                    # modal dialog, and wait for the worker to finish.
                    config = {"lang": "eng", "variant": "fast",
                              "preprocess": True, "scope": "page"}
                    controller._run_image_ocr(config, [0], None)
                    self._wait(application,
                               lambda: controller.layer() is not None, 30.0)
                    layer = controller.layer()
                    self.assertIsNotNone(layer, "OCR produced no layer")
                    text = " ".join(controller.page_text().split())
                    self.assertLess(O.cer(" ".join(" ".join(LINES).split()),
                                          text), 0.05)
                    self.assertEqual(controller.ocr_word_count(), len(layer))

                    # Same gestures as on a PDF now work on the picture.
                    qx, qy = self._word_center(controller, layer, "quick")
                    window._on_motion(self._event(qx, qy))
                    self.assertEqual(str(window.canvas.cget("cursor")),
                                     textui.CURSOR_TEXT)
                    self.assertTrue(controller.select_word_at(self._event(qx, qy)))
                    self.assertEqual(controller.selected_text(), "quick")
                    window._copy_shortcut()
                    self.assertEqual(copied, ["quick"])

                    # Searchable PDF next to the image, through the worker.
                    target = root / "figure.pdf"
                    stack.enter_context(mock.patch.object(
                        textui.filedialog, "asksaveasfilename",
                        lambda **kwargs: str(target)))
                    controller.save_searchable_pdf()
                    self._wait(application, target.exists, 20.0)
                    self.assertTrue(target.exists())
                    doc = D.fitz.open(str(target))
                    try:
                        self.assertTrue(doc[0].search_for("quick"))
                    finally:
                        doc.close()
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
