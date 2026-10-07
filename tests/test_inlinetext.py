"""Live Tk tests for the in-place text editor and the Add text tool.

Same harness as ``test_editui``: isolated settings, real application object,
generated fixture PDFs only. Keystrokes go through ``event_generate`` on the
canvas, so the editor's bindtag (and its shielding of window shortcuts) is
exercised exactly as in real use.
"""
from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import inlinetext as IT


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pdf(path: Path) -> None:
    fitz = D.fitz
    doc = fitz.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.draw_rect(fitz.Rect(40, 40, 555, 400), color=None, fill=(0.86, 0.93, 1.0))
    page.insert_text((72, 100), "Editable headline", fontname="hebo", fontsize=18)
    page.insert_text((72, 140), "A plain sentence to change.", fontsize=11)
    doc.new_page(width=595.28, height=841.89).insert_text(
        (72, 100), "Second page", fontsize=12)
    doc.save(str(path))
    doc.close()


def _ev(x: float, y: float, state: int = 0) -> SimpleNamespace:
    return SimpleNamespace(x=int(x), y=int(y), x_root=0, y_root=0, state=state)


def _spans(controller, page: int = 0):
    with controller.session.document._lock:
        raw = controller.session.document._doc[page].get_text("dict")
    return [(s["text"].replace("\xa0", " "), (round(s["origin"][0], 2), round(s["origin"][1], 2)))
            for b in raw["blocks"] for ln in b.get("lines", []) for s in ln["spans"]]


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class InlineTextTests(unittest.TestCase):

    def _start(self, stack, root: Path, target: Path):
        settings_dir = root / "settings"
        stack.enter_context(mock.patch.object(app_module, "SETTINGS_DIR", settings_dir))
        stack.enter_context(mock.patch.object(
            app_module, "SETTINGS_PATH", settings_dir / "settings.json"))
        stack.enter_context(mock.patch.object(
            app_module, "INSTANCE_PATH", settings_dir / "instance.json"))
        stack.enter_context(mock.patch.dict(app_module.DEFAULT_SETTINGS, {
            "single_instance": False, "thumb_size": 0,
            "geometry": "1100x760+40+40"}))
        try:
            application = app_module.App(root, target)
        except Exception as exc:
            self.skipTest(f"Tk cannot open a live window here: {exc}")
        return application

    @staticmethod
    def _wait(application, predicate, seconds: float = 4.0) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline and not predicate():
            application.root.update()
            time.sleep(0.005)

    @staticmethod
    def _keys(window, *keysyms: str) -> None:
        canvas = window.canvas
        canvas.focus_force()
        for keysym in keysyms:
            canvas.event_generate("<KeyPress>", keysym=keysym, when="now")
        window.update()

    def _canvas_point(self, window, x_pt: float, y_pt: float) -> tuple[float, float]:
        factor = window.edit._factor()
        return ((x_pt * factor - window.off_x) * window.scale,
                (y_pt * factor - window.off_y) * window.scale)

    def _open(self, stack, directory: str):
        root = Path(directory)
        pdf = root / "doc.pdf"
        _make_pdf(pdf)
        application = self._start(stack, root, pdf)
        window = application.windows[0]
        self._wait(application, lambda: window.document is not None)
        window.edit.activate()
        application.root.update()
        return application, window, pdf

    def test_text_tool_box_survives_redraws_and_commits_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            application, window, pdf = self._open(stack, directory)
            source_hash = _sha(pdf)
            try:
                controller = window.edit
                controller.set_tool("text")
                a = self._canvas_point(window, 300, 250)
                b = self._canvas_point(window, 480, 280)
                self.assertTrue(controller.on_press(_ev(*a)))
                controller.on_drag(_ev(*b))
                self.assertTrue(controller.on_release(_ev(*b)))
                editor = controller._inline
                self.assertIsNotNone(editor, "dragging a zone must open a text box")
                self.assertEqual(editor.mode, "new")
                self.assertIsNotNone(editor.wrap_width)

                # The original defect: every overlay redraw deleted the editor.
                window.redraw_now()
                application.root.update()
                self.assertIs(controller._inline, editor)
                self.assertTrue(window.canvas.find_withtag(IT.TAG))
                self.assertTrue(editor.bar.frame.winfo_exists())

                # Real key events; window shortcuts (arrows page, Delete...)
                # must not fire while typing.
                page_before = window.page
                self._keys(window, "H", "i", "space", "t", "h", "e", "r", "e",
                           "Left", "Left", "BackSpace", "Next", "Delete")
                self.assertEqual(editor.model.text, "Hi the")   # "Hi th|re" minus e, r
                self.assertEqual(window.page, page_before)
                self.assertEqual(controller.session.can_undo, False)

                # Escape discards: no history, nothing written.
                self._keys(window, "Escape")
                self.assertIsNone(controller._inline)
                self.assertFalse(controller.session.can_undo)

                # Reopen with a click, type, format, Ctrl+Enter commits once.
                controller.on_press(_ev(*a))
                controller.on_release(_ev(*a))
                editor = controller._inline
                self.assertIsNotNone(editor)
                self.assertIsNone(editor.wrap_width, "a click starts an auto-width line")
                self._keys(window, "O", "K")
                editor.apply_style(size=20, bold=True)
                window.canvas.event_generate("<Control-Return>", when="now")
                application.root.update()
                self.assertIsNone(controller._inline)
                self.assertTrue(controller.session.can_undo)
                spans = _spans(controller)
                self.assertIn("OK", [t for t, _o in spans])
                self.assertEqual(_sha(pdf), source_hash, "no disk write before save")
                # The style is remembered for the next text box.
                defaults = application.settings["annot_defaults"]
                self.assertEqual(defaults["text_size"], 20)
                self.assertTrue(defaults["text_bold"])
                controller.undo()
                self.assertNotIn("OK", [t for t, _o in _spans(controller)])
            finally:
                application.quit_all()

    def test_double_click_edits_existing_text_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            application, window, pdf = self._open(stack, directory)
            source_hash = _sha(pdf)
            try:
                controller = window.edit
                controller.set_tool("select")
                before = dict(_spans(controller))
                x, y = self._canvas_point(window, 120, 136)
                self.assertTrue(controller.on_double_click(_ev(x, y)))
                editor = controller._inline
                self.assertIsNotNone(editor)
                self.assertEqual(editor.mode, "block")
                self.assertEqual(editor.model.text, "A plain sentence to change.")
                self.assertFalse(editor.changed(), "opening alone changes nothing")
                # Unchanged commit: no history entry at all.
                editor.finish(commit=True)
                application.root.update()
                self.assertFalse(controller.session.can_undo)

                controller.on_double_click(_ev(x, y))
                editor = controller._inline
                self._keys(window, "End", "space", "o", "k")
                self.assertTrue(editor.model.text.endswith("change. ok"))
                # A click outside the box places the text.
                far = self._canvas_point(window, 400, 600)
                self.assertTrue(controller.on_press(_ev(*far)))
                application.root.update()
                self.assertIsNone(controller._inline)
                after = dict(_spans(controller))
                self.assertIn("A plain sentence to change. ok", after)
                self.assertEqual(after["A plain sentence to change. ok"],
                                 before["A plain sentence to change."],
                                 "the edited line keeps its exact origin")
                self.assertEqual(after["Editable headline"], before["Editable headline"])
                self.assertEqual(_sha(pdf), source_hash)
            finally:
                application.quit_all()

    def test_page_change_commits_and_typing_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            application, window, _pdf = self._open(stack, directory)
            try:
                controller = window.edit
                controller.set_tool("text")
                point = self._canvas_point(window, 100, 500)
                controller.on_press(_ev(*point))
                controller.on_release(_ev(*point))
                self.assertTrue(window._typing(), "window shortcuts are suspended")
                self._keys(window, "Z")
                window.change_pdf_page(1)
                application.root.update()
                self.assertIsNone(controller._inline)
                self.assertFalse(window._typing())
                self.assertIn("Z", [t for t, _o in _spans(controller, 0)])
            finally:
                application.quit_all()


if __name__ == "__main__":
    unittest.main()
