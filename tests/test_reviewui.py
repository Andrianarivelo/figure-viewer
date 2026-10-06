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
    def viewer(self, *, native_rotation=0, comment=None, sticky_note=False):
        """Ensure each real application closes before its temporary files vanish."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path = root / "review.pdf"
            helpers._make_pdf(path)
            if comment is not None:
                # Model a PDF received from another reviewer, before opening it.
                with D.fitz.open(path) as fixture:
                    page = fixture[0]
                    annotation = (page.add_text_annot((250, 150), comment) if sticky_note
                                  else page.add_highlight_annot(page.search_for("FigView")))
                    annotation.set_info(content=comment, title="External reviewer")
                    annotation.update()
                    fixture.saveIncr()
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

    def test_click_foreign_comment_displays_author_and_complete_scrollable_text(self):
        """Existing comments open in reading mode and never change source bytes."""
        comment = "Révision α 中文\n" + "Detailed reviewer feedback.\n" * 40
        for rotation in (0, 90):
            with self.subTest(rotation=rotation), self.viewer(
                    native_rotation=rotation, comment=comment) as (application, window, path):
                original = path.read_bytes()
                # Place the target in the canvas center, away from the minimap.
                window.fit_page()
                application.root.update()
                with D.fitz.open(path) as fixture:
                    page = fixture[0]
                    annotation = next(page.annots())
                    rect = tuple(annotation.rect)
                box = window.edit.points_rect_to_canvas(rect)
                event = self._event(window, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                window._on_press(event)
                window._on_release(event)
                review = window.edit.review
                self.indexed(application, review, 1)
                self.assertTrue(review.panel.winfo_manager())
                self.assertFalse(window.edit.active)
                self.assertEqual(review.selected_info().contents, comment)
                preview = review.preview.get("1.0", "end-1c")
                self.assertIn("By External reviewer", preview)
                self.assertIn(comment, preview)
                application.root.update()
                review.preview.yview_moveto(1)
                self.assertGreater(review.preview.yview()[0], 0)
                review.query.set("external reviewer")
                self.assertEqual(len(review.tree.get_children()), 1)
                self.assertFalse(window.edit.session.dirty)
                self.assertEqual(path.read_bytes(), original)

    def test_drag_over_foreign_comment_does_not_open_review_or_create_session(self):
        """Dragging over annotated text retains the text selection interaction."""
        with self.viewer(comment="Existing comment") as (_application, window, path):
            with D.fitz.open(path) as fixture:
                page = fixture[0]
                annotation = next(page.annots())
                box = window.edit.points_rect_to_canvas(tuple(annotation.rect))
            event = self._event(window, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            window._on_press(event)
            window._on_drag(event)
            window._on_release(event)
            self.assertIsNone(window.edit.review.panel)
            self.assertIsNone(window.edit.session)

    def test_click_imported_sticky_note_clears_filters_and_selects_comment(self):
        """A clicked note remains visible even after an unrelated review search."""
        with self.viewer(comment="Reviewer note", sticky_note=True) as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            review.query.set("unrelated search")
            review.kind.set("Highlights")
            info = review.rows[0]
            box = window.edit.points_rect_to_canvas(info.rect)
            event = self._event(window, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            window._on_press(event)
            window._on_release(event)
            self.indexed(application, review, 1)
            self.assertEqual(review.selected_info().contents, "Reviewer note")
            self.assertEqual(review.query.get(), "")
            self.assertEqual(review.kind.get(), "All")

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
                                       "quote": quote, "comment": "'" + changed,
                                       "author": "", "id": info.uid,
                                       "parent_id": "", "created": ""}])
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


    def test_inline_reply_thread_search_edit_undo_save_and_export(self):
        """Exercise the real composer against an imported PDF comment."""
        with self.viewer(comment="Please explain the method") as (application, window, path):
            original = path.read_bytes()
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            root = review.rows[0]
            root_key = review._key(root)
            review.tree.selection_set(root_key)
            review._selected()
            review.reply_button.invoke()
            self.assertTrue(review.composer.winfo_manager())
            review.author.set("Réviseur α")
            comment = "We added the requested control.\nUnicode 中文 🧠"
            review.reply_text.insert("1.0", comment)
            review.post_button.invoke()
            self.indexed(application, review, 2)
            reply = review.selected_info()
            self.assertIsNotNone(reply)
            self.assertEqual((reply.parent_uid, reply.contents, reply.author),
                             (root.uid, comment, "Réviseur α"))
            self.assertEqual(review.tree.parent(review._key(reply)), root_key)
            self.assertFalse(review.composer.winfo_manager())
            preview = review.preview.get("1.0", "end-1c")
            self.assertIn(root.contents, preview)
            self.assertIn(comment, preview)
            review.query.set("requested control")
            self.assertEqual(review.tree.get_children(), (root_key,))
            self.assertEqual(review.tree.get_children(root_key), (review._key(reply),))
            changed = "Updated reply text"
            with mock.patch.object(R.DLG, "ask_text", return_value=changed):
                review.edit_comment()
            self.indexed(application, review, 2)
            # Editing changes the search match; clearing the query restores
            # the conversation and preserves the original author metadata.
            review.query.set("")
            review.tree.selection_set(review._key(reply))
            self.assertEqual(review.selected_info().author, reply.author)
            window.edit.undo()
            self.indexed(application, review, 2)
            window.edit.undo()
            self.indexed(application, review, 1)
            window.edit.redo()
            self.indexed(application, review, 2)
            exported = path.with_suffix(".csv")
            with mock.patch.object(R.filedialog, "asksaveasfilename", return_value=str(exported)):
                review.export_csv()
            with exported.open(encoding="utf-8-sig", newline="") as stream:
                records = list(csv.DictReader(stream))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["parent_id"], root.uid)
            self.assertEqual(records[1]["comment"], comment)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(window.edit.save(synchronous=True))
            with D.fitz.open(path) as reopened:
                page = reopened[0]
                parent, saved_reply = list(page.annots())
                self.assertEqual(saved_reply.irt_xref, parent.xref)
                self.assertEqual(saved_reply.info["content"], comment)

    def test_reply_drafts_survive_selection_refresh_and_close(self):
        """Drafts are per-message and survive undo-driven asynchronous refresh."""
        with self.viewer(comment="First thread") as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            first = review.rows[0]
            second_uid = window.edit.session.add_note(0, (250, 200), "Second thread")
            self.indexed(application, review, 2)
            first_key, second_key = review._key(first), f"0:{second_uid}"
            review.tree.selection_set(first_key)
            review._selected()
            review.start_reply()
            review.reply_text.insert("1.0", "First draft")
            review.tree.selection_set(second_key)
            review._selected()
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "")
            review.reply_text.insert("1.0", "Second draft")
            review.tree.selection_set(first_key)
            review._selected()
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "First draft")
            review.refresh()
            self.indexed(application, review, 2)
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "First draft")
            review.close_panel()
            review.open_panel()
            self.indexed(application, review, 2)
            review.tree.selection_set(second_key)
            review._selected()
            review.start_reply()
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "Second draft")
            review.cancel_reply()
            self.assertFalse(review.composer.winfo_manager())

    def test_empty_reply_and_failed_reply_keep_document_and_draft(self):
        """Validation and failed operations must not erase a composed response."""
        with self.viewer(comment="Root comment") as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            review.tree.selection_set(review.tree.get_children()[0])
            review._selected()
            review.start_reply()
            review.reply_text.insert("1.0", " \n ")
            review.post_reply()
            self.assertFalse(window.edit.session.dirty)
            review.reply_text.delete("1.0", "end")
            review.reply_text.insert("1.0", "Keep this draft")
            with mock.patch.object(window.edit.session, "add_reply", side_effect=RuntimeError("failed")), \
                    mock.patch.object(R.messagebox, "showerror"):
                review.post_reply()
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "Keep this draft")
            self.assertFalse(window.edit.session.dirty)

    def test_theme_rebuild_keeps_reply_draft_in_memory(self):
        """A palette change rebuilds widgets without losing unpublished text."""
        with self.viewer(comment="Root comment") as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            key = review.tree.get_children()[0]
            review.tree.selection_set(key)
            review._selected()
            review.start_reply()
            review.reply_text.insert("1.0", "Draft before switching theme")
            application.toggle_theme()
            replacement = application.windows[0]
            self._wait(application, lambda: replacement.document is not None)
            review = replacement.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            review.tree.selection_set(key)
            review._selected()
            review.start_reply()
            self.assertEqual(review.reply_text.get("1.0", "end-1c"), "Draft before switching theme")

    def test_nested_thread_delete_and_root_navigation(self):
        """Nested replies stay grouped, navigate to the root and delete atomically."""
        with self.viewer(comment="Root comment") as (application, window, _path):
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            root = review.rows[0]
            session = window.edit.session
            first = session.add_reply(0, root.uid, "First reply", "Reviewer A")
            second = session.add_reply(0, first, "Nested reply", "Reviewer B")
            self.indexed(application, review, 3)
            root_key = review._key(root)
            self.assertEqual(len(review.tree.get_children(root_key)), 2)
            review.tree.selection_set(f"0:{second}")
            review._selected()
            self.assertEqual(window.edit._selected_uid, root.uid)
            review.tree.selection_set(root_key)
            review._selected()
            self.assertEqual(review.delete_button.cget("text"), "Delete thread")
            review.delete_button.invoke()
            self.indexed(application, review, 0)
            window.edit.undo()
            self.indexed(application, review, 3)


    def test_view_menu_review_checkmark_and_reply_access(self):
        """The real View checkbutton opens/closes the pane and exposes Reply."""
        with self.viewer(comment="Review this passage") as (application, window, _path):
            review = window.edit.review
            menu = window.menubar.menus["View"]
            index = next(i for i in range(menu.index("end") + 1)
                         if menu.type(i) != "separator"
                         and menu.entrycget(i, "label") == "Review Panel")
            self.assertEqual(menu.type(index), "checkbutton")
            self.assertEqual(menu.entrycget(index, "accelerator"), "Ctrl+Alt+R")
            menu.invoke(index)
            self.indexed(application, review, 1)
            self.assertTrue(review.visible.get())
            review.close_panel()
            self.assertFalse(review.visible.get())
            menu.invoke(index)
            self.indexed(application, review, 1)
            review.tree.selection_set(review.tree.get_children()[0])
            review._selected()
            review.reply_button.invoke()
            self.assertTrue(review.composer.winfo_manager())
            menu.invoke(index)
            self.assertFalse(review.visible.get())
            self.assertFalse(review.panel.winfo_manager())

    def test_unseen_read_replied_colors_and_new_reply_priority(self):
        """New or edited messages become unseen without changing raw PDF bytes."""
        with self.viewer(comment="Original comment") as (application, window, path):
            original = path.read_bytes()
            review = window.edit.review
            review.open_panel()
            self.indexed(application, review, 1)
            root = review.rows[0]
            root_key = review._key(root)
            self.assertEqual(review.thread_state(root_key), "Unseen")
            self.assertEqual(review.tree.item(root_key, "tags"), ("Unseen",))
            review.tree.selection_set(root_key)
            review._selected()
            self.assertEqual(review.thread_state(root_key), "Read")
            self.assertFalse(window.edit.session.dirty)
            from figureviewer import app as app_module
            saved = app_module.load_settings()
            self.assertEqual(saved["review_seen"], application.settings["review_seen"])
            # Make the thread inactive before a new reply arrives.
            review.tree.selection_remove(root_key)
            reply = window.edit.session.add_reply(0, root.uid, "First response", "Reviewer")
            self.indexed(application, review, 2)
            self.assertEqual(review.thread_state(root_key), "Unseen")
            review.tree.selection_set(root_key)
            review._selected()
            self.assertEqual(review.thread_state(root_key), "Replied")
            self.assertEqual(review.tree.item(root_key, "tags"), ("Replied",))
            review.tree.selection_remove(root_key)
            window.edit.session.set_annot_props(0, reply, contents="Changed response")
            self.indexed(application, review, 2)
            self.assertEqual(review.thread_state(root_key), "Unseen")
            review.tree.selection_set(f"0:{reply}")
            review._selected()
            self.assertEqual(review.thread_state(root_key), "Replied")
            self.assertEqual(len(set(review.state_colors.values())), 3)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
