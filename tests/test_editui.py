"""Live Tk tests for the PDF edit mode: gestures, save, truce, rebuild.

These drive the real application object the way test_gui.py does: an isolated
settings directory, deadline-polling instead of sleeps, and quit_all in a
finally. The assertions mirror the non-negotiable invariants: the file on
disk never changes until an explicit save, a save is atomic and reconciled
with the watcher, and a dirty session survives navigation and the theme
toggle's window rebuild.
"""
from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import pdfedit as PE


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pdf(path: Path, *, with_form: bool = False) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.insert_text((72, 100), "FigView edit mode fixture", fontsize=16)
    page.insert_text((72, 140), "A second line for markup sweeps.", fontsize=11)
    if with_form:
        for index, name in enumerate(("first_name", "last_name", "email")):
            widget = pymupdf.Widget()
            widget.field_name = name
            widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            widget.rect = pymupdf.Rect(200, 300 + 40 * index,
                                       420, 322 + 40 * index)
            widget.field_value = ""
            page.add_widget(widget)
        box = pymupdf.Widget()
        box.field_name = "subscribe"
        box.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        box.rect = pymupdf.Rect(200, 430, 218, 448)
        box.field_value = False
        page.add_widget(box)
    doc.new_page(width=595.28, height=841.89).insert_text(
        (72, 100), "Second page", fontsize=12)
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class EditModeTests(unittest.TestCase):
    def _start(self, stack, root: Path, target: Path):
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
    def _wait(application, predicate, seconds: float = 4.0) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline and not predicate():
            application.root.update()
            time.sleep(0.005)

    def _event(self, window, x: float, y: float) -> SimpleNamespace:
        return SimpleNamespace(x=int(x), y=int(y), x_root=0, y_root=0, state=0)

    def test_edit_mode_end_to_end(self) -> None:
        from contextlib import ExitStack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "doc.pdf"
            _make_pdf(pdf_path)
            source_hash = _sha(pdf_path)

            with ExitStack() as stack:
                application = self._start(stack, root, pdf_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    self.assertIsNotNone(window.document)

                    # --- enter edit mode: the document object is swapped for
                    # the session's editable one, palette appears.
                    window.edit.activate()
                    application.root.update()
                    self.assertTrue(window.edit.active)
                    self.assertIsNotNone(window.edit.session)
                    self.assertIsInstance(window.document,
                                          PE.EditableVectorDocument)
                    self.assertTrue(window.edit.palette_row.winfo_ismapped())

                    # --- highlight the headline through the real event path.
                    window.edit.set_tool("highlight")
                    controller = window.edit
                    c0 = controller.points_rect_to_canvas((70, 84, 300, 108))
                    press = self._event(window, c0[0], c0[1])
                    release = self._event(window, c0[2], c0[3])
                    self.assertTrue(controller.on_press(press))
                    controller.on_drag(release)
                    self.assertTrue(controller.on_release(release))
                    annots = controller.session.annots_on(0)
                    self.assertEqual([a.kind for a in annots], [PE.TYPE_HIGHLIGHT])
                    self.assertEqual(_sha(pdf_path), source_hash,
                                     "no mutation may reach the disk before save")
                    self.assertTrue(window.edit.dirty)
                    self.assertIn("●", window.title())

                    # --- undo/redo through the controller.
                    controller.undo()
                    self.assertEqual(controller.session.annots_on(0), [])
                    self.assertFalse(window.edit.dirty)
                    controller.redo()
                    self.assertEqual(len(controller.session.annots_on(0)), 1)

                    # --- atomic save: disk changes, session cleans, watcher
                    # adopts our own stamp instead of reloading.
                    self.assertTrue(window.edit.save(synchronous=True))
                    application.root.update()
                    self.assertNotEqual(_sha(pdf_path), source_hash)
                    self.assertFalse(window.edit.dirty)
                    self.assertNotIn("●", window.title())
                    reopened = D.fitz.open(str(pdf_path))
                    try:
                        held_page = reopened[0]
                        kinds = [a.type[0] for a in held_page.annots()]
                        self.assertEqual(kinds, [D.fitz.PDF_ANNOT_HIGHLIGHT])
                    finally:
                        reopened.close()
                    stat = pdf_path.stat()
                    fresh = D.Entry(pdf_path, stat.st_size, stat.st_mtime,
                                    D.KIND_DOC)
                    self.assertEqual(window.edit.reload_decision(fresh), "adopt")

                    # --- an external change while dirty is blocked, never
                    # silently reloaded over unsaved edits. The bytes on
                    # disk really change here: the controller's content-hash
                    # tiebreaker must see foreign bytes, not our own save.
                    controller.session.add_note(0, (300, 200), "unsaved")
                    self.assertTrue(window.edit.dirty)
                    with open(pdf_path, "ab") as handle:
                        handle.write(b"\n% external writer\n")
                    ext_stat = pdf_path.stat()
                    external = D.Entry(pdf_path, ext_stat.st_size,
                                       ext_stat.st_mtime + 99.0, D.KIND_DOC)
                    self.assertEqual(window.edit.reload_decision(external),
                                     "block")

                    # --- the theme toggle rebuilds every window; the dirty
                    # session, its undo depth and the marker must survive.
                    depth_before = len(controller.session._history.undo)
                    application.toggle_theme()
                    window = application.windows[0]
                    self._wait(application,
                               lambda: window.document is not None)
                    self._wait(application,
                               lambda: window.edit.session is not None)
                    self.assertIsNotNone(window.edit.session)
                    self.assertTrue(window.edit.dirty)
                    self.assertEqual(
                        len(window.edit.session._history.undo), depth_before)
                    self.assertIsInstance(window.document,
                                          PE.EditableVectorDocument)
                    self.assertIn("●", window.title())

                    # --- page operations are structural and undoable.
                    window.edit.rotate_page(0, 90)
                    self._wait(application, lambda: True, 0.1)
                    with window.edit.session.document._lock:
                        rotation = window.edit.session.document._page(0).rotation
                    self.assertEqual(rotation, 90)
                    window.edit.session.undo()
                    window.edit.delete_pages([1])
                    self.assertEqual(window.edit.session.document.page_count, 1)
                    window.edit.session.undo()
                    self.assertEqual(window.edit.session.document.page_count, 2)

                    # --- closing with unsaved edits asks; discard closes the
                    # session and the window.
                    from figureviewer import editui as editui_module
                    with mock.patch.object(editui_module.DLG,
                                           "ask_save_discard_cancel",
                                           return_value="discard"):
                        window.close()
                    application.root.update()
                    self.assertEqual(application.edit_sessions, {})
                finally:
                    application.quit_all()

    def test_form_bar_and_field_editing(self) -> None:
        from contextlib import ExitStack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "form.pdf"
            _make_pdf(pdf_path, with_form=True)
            source_hash = _sha(pdf_path)

            with ExitStack() as stack:
                application = self._start(stack, root, pdf_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    self._wait(application,
                               lambda: window.edit.form_bar is not None
                               and window.edit.form_bar.winfo_ismapped())
                    self.assertTrue(window.edit.form_bar.winfo_ismapped(),
                                    "a form PDF must reveal the form bar")

                    # Clicking a text field opens the overlay editor even
                    # outside edit mode; committing writes the value.
                    fields = window.edit._form_fields()
                    first = next(f for f in fields if f.name == "first_name")
                    c0 = window.edit.points_rect_to_canvas(first.rect_pts)
                    event = self._event(window, (c0[0] + c0[2]) / 2,
                                        (c0[1] + c0[3]) / 2)
                    self.assertTrue(window.edit.on_press(event))
                    self.assertIsNotNone(window.edit._editor)
                    window.edit._editor["widget"].insert("1.0", "Andry")
                    window.edit.commit_open_editor()
                    session = window.edit.session
                    self.assertIsNotNone(session)
                    with session.document._lock:
                        page_proxy, widget = session._locate_widget(0, first.xref)
                        self.assertEqual(widget.field_value, "Andry")
                        del page_proxy, widget

                    # A checkbox toggles on click, and one undo reverts it.
                    window.edit._fields_cache = None
                    box = next(f for f in window.edit._form_fields()
                               if f.ftype == "checkbox")
                    c1 = window.edit.points_rect_to_canvas(box.rect_pts)
                    self.assertTrue(window.edit.on_press(self._event(
                        window, (c1[0] + c1[2]) / 2, (c1[1] + c1[3]) / 2)))
                    with session.document._lock:
                        page_proxy, widget = session._locate_widget(0, box.xref)
                        self.assertTrue(bool(widget.field_value))
                        del page_proxy, widget

                    # Auto-fill: preset + review dialog (patched to accept all)
                    # fills matching fields in ONE undo entry.
                    store = window.edit._identities()
                    store.add("Test", {"first_name": "Andriana",
                                       "last_name": "Rivelo",
                                       "email": "nnjiva@example.org"})
                    from figureviewer import editui as editui_module

                    class _AcceptAll:
                        def __init__(self, _parent, _palette, proposals):
                            self._proposals = proposals

                        def wait(self):
                            return self._proposals

                    with mock.patch.object(editui_module.DLG,
                                           "AutoFillReviewDialog", _AcceptAll):
                        depth_before = len(session._history.undo)
                        window.edit.auto_fill()
                    self.assertEqual(len(session._history.undo),
                                     depth_before + 1,
                                     "auto-fill must be one undo entry")
                    with session.document._lock:
                        page_proxy, widget = session._locate_widget(
                            0, next(f for f in fields
                                    if f.name == "email").xref)
                        self.assertEqual(widget.field_value,
                                         "nnjiva@example.org")
                        del page_proxy, widget

                    self.assertEqual(_sha(pdf_path), source_hash)
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
