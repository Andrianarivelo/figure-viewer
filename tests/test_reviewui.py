"""Live Tk review regressions using generated PDFs and isolated settings."""
from __future__ import annotations

import csv
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from figureviewer import docs as D
from figureviewer import reviewui as R
from tests import test_editui as helpers


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for live review tests")
class ReviewUITests(unittest.TestCase):
    """Compose existing Tk setup helpers without inheriting their test methods."""

    _start = helpers.EditModeTests._start
    _wait = staticmethod(helpers.EditModeTests._wait)
    _event = helpers.EditModeTests._event

    @contextmanager
    def viewer(self, *, native_rotation=0):
        """Ensure each real application closes before its temporary files vanish."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path = root / "review.pdf"
            helpers._make_pdf(path)
            if native_rotation:
                # Only mutate our generated fixture before the viewer opens it.
                with D.fitz.open(path) as fixture:
                    fixture[0].set_rotation(native_rotation)
                    fixture.saveIncr()
            application = self._start(stack, root, path)
            try:
                window = application.windows[0]
                self._wait(application, lambda: window.document is not None)
                self.assertIsNotNone(window.document)
                application.root.update()
                self.assertIsNotNone(window.text.layer())
                yield application, window, path
            finally:
                application.quit_all()

    def selected_text(self, window):
        """Select both fixture lines and return independent quote/geometry data."""
        window.text.select_all()
        layer = window.text.layer()
        factor = window.edit._factor()
        rectangles = [tuple(value / factor for value in rect)
                      for rect in layer.rects_for(window.text.selected)]
        self.assertEqual(len(rectangles), 2)
        return window.text.selected_text(), rectangles

    def indexed(self, application, review, count):
        """Wait until the asynchronous annotation index has finished refreshing."""
        self._wait(application, lambda: review._job is None and len(review.rows) == count)
        self.assertIsNone(review._job)
        self.assertEqual(len(review.rows), count)

    def test_toolbar_highlight_captures_multiline_selection_before_document_swap(self):
        """Click the real toolbar button while the original text layer is active."""
        with self.viewer() as (application, window, path):
            original = path.read_bytes()
            document = window.document
            quote, rectangles = self.selected_text(window)
            window.edit.review._buttons[0].invoke()
            self.assertIsNot(window.document, document)
            session = window.edit.session
            info, = session.annots_on(0)
            self.assertEqual(info.subject, quote)
            self.assertEqual(info.contents, "")
            self.assertEqual(info.kind, "highlight")
            with session.document._lock:
                page, annotation = session._locate(0, info.uid)
                vertices = annotation.vertices
                self.assertEqual(len(vertices), 8)
                for offset, expected in zip((0, 4), rectangles):
                    points = vertices[offset:offset + 4]
                    actual = (min(p[0] for p in points), min(p[1] for p in points),
                              max(p[0] for p in points), max(p[1] for p in points))
                    for observed, wanted in zip(actual, expected):
                        self.assertAlmostEqual(observed, wanted, places=3)
            self.assertFalse(window.text.has_selection())
            self.assertFalse(window.edit.active)
            self.assertEqual(path.read_bytes(), original)
            window.edit.undo()
            self.assertEqual(session.annots_on(0), [])
            self.assertFalse(session.can_undo)
            window.edit.redo()
            self.assertEqual(session.annots_on(0)[0].subject, quote)

    def test_comment_cancel_or_blank_does_not_create_session_or_clear_selection(self):
        """A cancelled dialog must leave the existing viewer and selection intact."""
        with self.viewer() as (_application, window, path):
            original = path.read_bytes()
            quote, _rectangles = self.selected_text(window)
            document = window.document
            for answer in (None, "", " \n\t "):
                with self.subTest(answer=answer), mock.patch.object(R.DLG, "ask_text", return_value=answer):
                    window.edit.review._buttons[1].invoke()
                    self.assertIsNone(window.edit.session)
                    self.assertIs(window.document, document)
                    self.assertEqual(window.text.selected_text(), quote)
                    self.assertEqual(path.read_bytes(), original)

    def test_comment_index_search_edit_delete_undo_save_and_csv_export(self):
        """Review actions and export use the same persistent, undoable PDF data."""
        with self.viewer() as (application, window, path):
            quote, _rectangles = self.selected_text(window)
            comment = "À vérifier : α, 中文 🧠\nDeuxième ligne"
            review = window.edit.review
            with mock.patch.object(R.DLG, "ask_text", return_value=comment) as dialog:
                review._buttons[1].invoke()
            self.assertTrue(dialog.call_args.kwargs["multiline"])
            self.indexed(application, review, 1)
            self.assertTrue(review.panel.winfo_manager())
            info, = review.rows
            self.assertEqual((info.subject, info.contents), (quote, comment))
            review.query.set("À VÉRIFIER")
            self.assertEqual(len(review.tree.get_children()), 1)
            review.query.set("markup sweeps")
            self.assertEqual(len(review.tree.get_children()), 1)
            review.query.set("absent search")
            self.assertEqual(len(review.tree.get_children()), 0)
            review.query.set("")
            key, = review.tree.get_children()
            review.tree.selection_set(key)
            changed = "=HYPERLINK(\"example\")\nÉdité"
            with mock.patch.object(R.DLG, "ask_text", return_value=changed):
                review.edit_comment()
            self.indexed(application, review, 1)
            self.assertEqual(review.rows[0].contents, changed)
            key, = review.tree.get_children()
            review.tree.selection_set(key)
            exported = path.with_suffix(".csv")
            with mock.patch.object(R.filedialog, "asksaveasfilename", return_value=str(exported)):
                review.export_csv()
            with exported.open(encoding="utf-8-sig", newline="") as stream:
                records = list(csv.DictReader(stream))
            self.assertEqual(records, [{"page": "1", "type": "highlight",
                                       "quote": quote, "comment": "'" + changed}])
            review.delete_selected()
            self.indexed(application, review, 0)
            window.edit.undo()
            self.indexed(application, review, 1)
            self.assertEqual(review.rows[0].contents, changed)
            window.edit.undo()
            self.indexed(application, review, 1)
            self.assertEqual(review.rows[0].contents, comment)
            self.assertTrue(window.edit.save(synchronous=True))
            with D.fitz.open(path) as reopened:
                page = reopened[0]
                annotation, = list(page.annots())
                self.assertEqual(annotation.info["content"], comment)
                self.assertEqual(annotation.info["subject"], quote)

    def test_sticky_highlight_drags_use_real_viewer_gestures(self):
        """Repeated text drags create separate highlights without opening Edit."""
        with self.viewer() as (application, window, _path):
            review = window.edit.review
            review.action("highlight")
            self.assertEqual(review.mode.get(), "highlight")
            for expected_count in (1, 2):
                layer = window.text.layer()
                first = window.text._source_rect_to_canvas(layer.words[0].rect)
                last = window.text._source_rect_to_canvas(layer.words[3].rect)
                press = self._event(window, (first[0] + first[2]) / 2,
                                    (first[1] + first[3]) / 2)
                release = self._event(window, (last[0] + last[2]) / 2,
                                      (last[1] + last[3]) / 2)
                window._on_press(press)
                window._on_drag(release)
                window._on_release(release)
                self.assertIsNotNone(window.edit.session)
                self.assertEqual(len(window.edit.session.annots_on(0)), expected_count)
                self.assertEqual(review.mode.get(), "highlight")
                self.assertFalse(window.edit.active)
                application.root.update()
            self.assertTrue(review.cancel_mode())
            self.assertEqual(review.mode.get(), "")

    def test_direct_panel_destroy_cancels_pending_tk_index_job(self):
        """Root teardown must not leave a callback to a destroyed Tcl command."""
        with self.viewer() as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            job = review._job
            self.assertIsNotNone(job)
            self.assertIn(job, application.root.tk.call("after", "info"))
            review.panel.destroy()
            self.assertIsNone(review._job)
            self.assertNotIn(job, application.root.tk.call("after", "info"))
            application.root.update()

    def test_color_and_opacity_refresh_preserves_selected_annotation(self):
        """Sequential review styling actions continue to target the same PDF uid."""
        with self.viewer() as (application, window, _path):
            quote, _rectangles = self.selected_text(window)
            review = window.edit.review
            uid = review.annotate_selection()
            review.open_panel()
            self.indexed(application, review, 1)
            review.tree.selection_set(f"0:{uid}")
            review.set_color("#5aa7f0")
            self.indexed(application, review, 1)
            self.assertIsNotNone(review.selected_info())
            self.assertEqual(review.selected_info().uid, uid)
            review.opacity.set("75")
            review.set_opacity()
            self.indexed(application, review, 1)
            self.assertIsNotNone(review.selected_info())
            self.assertEqual(review.selected_info().uid, uid)
            self.assertEqual(review.selected_info().subject, quote)
            session = window.edit.session
            with session.document._lock:
                page, annotation = session._locate(0, uid)
                for observed, expected in zip(annotation.colors["stroke"],
                                              (90 / 255, 167 / 255, 240 / 255)):
                    self.assertAlmostEqual(observed, expected, places=5)
                self.assertAlmostEqual(annotation.opacity, 0.75, places=5)

    def test_rotated_flipped_text_drag_preserves_pdf_highlight_geometry(self):
        """Display transformations must not rotate or mirror stored PDF quads."""
        with self.viewer() as (_application, window, _path):
            window.flip_h_var().set(True)
            window.rotate(90)
            layer = window.text.layer()
            indices = list(range(4))
            quote = layer.text_for(indices)
            factor = window.edit._factor()
            expected, = [tuple(value / factor for value in rect)
                         for rect in layer.rects_for(indices)]
            first = window.text._source_rect_to_canvas(layer.words[0].rect)
            last = window.text._source_rect_to_canvas(layer.words[3].rect)
            press = self._event(window, (first[0] + first[2]) / 2,
                                (first[1] + first[3]) / 2)
            release = self._event(window, (last[0] + last[2]) / 2,
                                  (last[1] + last[3]) / 2)
            window._on_press(press)
            window._on_drag(release)
            window._on_release(release)
            self.assertEqual(window.text.selected_text(), quote)
            window.edit.review._buttons[0].invoke()
            session = window.edit.session
            info, = session.annots_on(0)
            self.assertEqual(info.subject, quote)
            with session.document._lock:
                page, annotation = session._locate(0, info.uid)
                points = annotation.vertices
                self.assertEqual(len(points), 4)
                actual = (min(p[0] for p in points), min(p[1] for p in points),
                          max(p[0] for p in points), max(p[1] for p in points))
                for observed, wanted in zip(actual, expected):
                    self.assertAlmostEqual(observed, wanted, places=3)

    def test_native_pdf_rotation_stores_unrotated_text_markup_vertices(self):
        """PDF /Rotate affects display words, but annotation quads stay unrotated."""
        with self.viewer(native_rotation=90) as (_application, window, path):
            with D.fitz.open(path) as fixture:
                page = fixture[0]
                expected = [tuple(page.search_for(line)[0]) for line in (
                    "FigView edit mode fixture", "A second line for markup sweeps.")]
            window.text.select_all()
            quote = window.text.selected_text()
            window.edit.review._buttons[0].invoke()
            self.assertIsNotNone(window.edit.session)
            with D.fitz.open(stream=window.edit.session.save_bytes(), filetype="pdf") as saved:
                page = saved[0]
                self.assertEqual(page.rotation, 90)
                annotation, = list(page.annots())
                self.assertEqual(annotation.info["subject"], quote)
                vertices = annotation.vertices
                self.assertEqual(len(vertices), 8)
                for offset, expected_rect in zip((0, 4), expected):
                    points = vertices[offset:offset + 4]
                    actual = (min(p[0] for p in points), min(p[1] for p in points),
                              max(p[0] for p in points), max(p[1] for p in points))
                    for observed, wanted in zip(actual, expected_rect):
                        self.assertAlmostEqual(observed, wanted, places=3)

    def test_review_focus_maps_native_rotated_annotation(self):
        """Review navigation targets displayed geometry and point mapping round-trips."""
        with self.viewer(native_rotation=90) as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            session = window.edit.session
            with session.document._lock:
                quads = session._page(0).search_for("FigView", quads=True)
            uid = session.add_markup(0, quads, "highlight", (1, 0.8, 0.2), 0.35)
            self.indexed(application, review, 1)
            review.tree.selection_set(f"0:{uid}")
            info = review.selected_info()
            with session.document._lock:
                page = session._page(0)
                displayed = D.fitz.Rect(info.rect) * page.rotation_matrix
            factor = window.edit._factor()
            box = window.text._source_rect_to_canvas(tuple(value * factor for value in displayed))
            width, height = window._canvas_size()
            expected_pan = ((box[0] + box[2] - width) / 2,
                            (box[1] + box[3] - height) / 2)
            with mock.patch.object(window, "_pan_by", wraps=window._pan_by) as pan:
                review._selected()
            for observed, wanted in zip(pan.call_args.args, expected_pan):
                self.assertAlmostEqual(observed, wanted, places=3)
            # Rendering may clamp the pan near page edges; independently verify
            # both coordinate conversions at the resulting viewport position.
            box = window.edit.points_rect_to_canvas(info.rect)
            point = window.edit.canvas_to_points((box[0] + box[2]) / 2,
                                                  (box[1] + box[3]) / 2)
            self.assertAlmostEqual(point[0], (info.rect[0] + info.rect[2]) / 2, places=3)
            self.assertAlmostEqual(point[1], (info.rect[1] + info.rect[3]) / 2, places=3)


if __name__ == "__main__":
    unittest.main()
