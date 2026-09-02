"""Edit-session kernel tests: integrity, undo/redo, budgets, forms, pages.

Everything runs headless against PDFs generated on the fly; no user file is
ever read or written. The one non-negotiable assertion, repeated across the
class: the bytes of the source file on disk never change until an explicit
save, and this suite never saves onto the fixture.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from figureviewer import docs as D
from figureviewer import pdfedit as E


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _render_digest(session: E.EditSession, page: int = 0) -> str:
    """Content fingerprint: rendered pixels + text + structure, not raw bytes.

    Raw byte equality is the wrong test for snapshot restores, because a
    PyMuPDF open/serialise round trip may legally reorder objects; what undo
    must guarantee is that the *document* is the same, which is pixels, text,
    page geometry and annotation census."""
    document = session.document
    with document._lock:
        doc = document._doc
        pg = doc[page]
        pix = pg.get_pixmap(dpi=72, annots=True)
        annots = [(a.type[0], tuple(round(v, 2) for v in a.rect)) for a in pg.annots()]
        payload = (pix.samples, pg.get_text().encode(), repr(annots).encode(),
                   repr((doc.page_count, pg.rotation, tuple(pg.rect))).encode())
    digest = hashlib.sha256()
    for part in payload:
        digest.update(part)
    return digest.hexdigest()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for edit-session tests")
class EditSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "fixture.pdf"
        doc = D.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "FigView editing fixture", fontsize=16)
        page.insert_text((72, 140), "A sentence that will be marked up.", fontsize=11)
        doc.new_page(width=595, height=842).insert_text((72, 100), "Second page")
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)

    def _assert_source_untouched(self) -> None:
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "the raw file on disk must never change")

    # -- integrity and dirty tracking -------------------------------------
    def test_new_session_is_clean_and_reads_the_document(self) -> None:
        self.assertFalse(self.session.dirty)
        self.assertEqual(self.session.document.page_count, 2)
        self.assertIn("editing fixture", self.session.page_text(0))
        self._assert_source_untouched()

    def test_every_mutation_leaves_the_source_file_untouched(self) -> None:
        page = 0
        self.session.add_note(page, (300, 100), "note", icon="Comment")
        self.session.add_ink(page, [[(100, 400), (150, 380), (200, 420)]],
                             color=(0.8, 0.1, 0.1), width=2.0)
        self.session.rotate_page(1, 90)
        self.session.undo()
        self.session.save_bytes()
        self._assert_source_untouched()

    def test_dirty_follows_undo_back_to_the_save_point(self) -> None:
        self.session.add_note(0, (300, 100), "first")
        self.assertTrue(self.session.dirty)
        self.session.undo()
        self.assertFalse(self.session.dirty)
        self.session.redo()
        self.assertTrue(self.session.dirty)
        # Simulate a save at this position: dirty clears, further edits
        # re-set it, undoing back to the marker clears it again.
        self.session.mark_saved(_entry(self.pdf))
        self.assertFalse(self.session.dirty)
        self.session.add_note(0, (320, 120), "second")
        self.assertTrue(self.session.dirty)
        self.session.undo()
        self.assertFalse(self.session.dirty)

    # -- annotation round trips -------------------------------------------
    def test_markup_add_undo_redo_round_trip(self) -> None:
        page = 0
        with self.session.document._lock:
            quads = self.session._page(page).search_for("marked up", quads=True)
        self.assertTrue(quads)
        before = _render_digest(self.session)
        uid = self.session.add_markup(page, quads, E.TYPE_HIGHLIGHT,
                                      color=(1.0, 0.8, 0.2), opacity=0.35)
        annots = self.session.annots_on(page)
        self.assertEqual([a.kind for a in annots], [E.TYPE_HIGHLIGHT])
        self.assertEqual(annots[0].uid, uid)
        self.session.undo()
        self.assertEqual(self.session.annots_on(page), [])
        self.assertEqual(_render_digest(self.session), before)
        self.session.redo()
        restored = self.session.annots_on(page)
        self.assertEqual([a.kind for a in restored], [E.TYPE_HIGHLIGHT])
        self.assertEqual(restored[0].uid, uid, "uid must survive undo/redo")
        self._assert_source_untouched()

    def test_every_annotation_kind_is_created_with_its_own_type(self) -> None:
        page = 0
        self.session.add_free_text(page, (320, 90, 520, 130), "free text")
        self.session.add_note(page, (540, 100), "sticky")
        self.session.add_ink(page, [[(100, 400), (150, 380), (200, 420)]],
                             color=(0.8, 0.1, 0.1), width=2.0)
        self.session.add_shape(page, E.TYPE_RECT, (320, 380, 420, 430),
                               stroke=(0.0, 0.5, 0.0))
        self.session.add_shape(page, E.TYPE_ELLIPSE, (430, 380, 520, 430),
                               stroke=(0.0, 0.0, 0.8))
        self.session.add_shape(page, E.TYPE_LINE, ((100, 460), (250, 500)),
                               stroke=(0.1, 0.1, 0.1))
        self.session.add_shape(page, E.TYPE_ARROW, ((100, 520), (250, 560)),
                               stroke=(0.1, 0.1, 0.1))
        kinds = sorted(a.kind for a in self.session.annots_on(page))
        self.assertEqual(kinds, sorted([
            E.TYPE_FREETEXT, E.TYPE_NOTE, E.TYPE_INK, E.TYPE_RECT,
            E.TYPE_ELLIPSE, E.TYPE_LINE, E.TYPE_ARROW,
        ]))
        self._assert_source_untouched()

    def test_hit_test_returns_topmost_annotation(self) -> None:
        page = 0
        below = self.session.add_shape(page, E.TYPE_RECT, (100, 300, 300, 500),
                                       stroke=(0.0, 0.5, 0.0))
        above = self.session.add_shape(page, E.TYPE_RECT, (150, 350, 250, 450),
                                       stroke=(0.8, 0.1, 0.1))
        hit = self.session.hit_test(page, (200, 400))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.uid, above)
        edge = self.session.hit_test(page, (110, 310))
        self.assertEqual(edge.uid, below)
        self.assertIsNone(self.session.hit_test(page, (580, 820)))

    def test_move_and_resize_round_trip_for_box_and_geometry_annots(self) -> None:
        page = 0
        box_uid = self.session.add_shape(page, E.TYPE_RECT, (100, 300, 200, 360),
                                         stroke=(0.0, 0.5, 0.0))
        ink_uid = self.session.add_ink(page, [[(100, 400), (150, 380), (200, 420)]],
                                       color=(0.8, 0.1, 0.1), width=2.0)
        arrow_uid = self.session.add_shape(page, E.TYPE_ARROW,
                                           ((300, 400), (400, 480)),
                                           stroke=(0.1, 0.1, 0.1))
        for uid, delta in ((box_uid, 40.0), (ink_uid, 25.0), (arrow_uid, 30.0)):
            info = next(a for a in self.session.annots_on(page) if a.uid == uid)
            old = info.rect
            moved = (old[0] + delta, old[1] + delta, old[2] + delta, old[3] + delta)
            self.session.move_annot(page, uid, moved)
            after = next(a for a in self.session.annots_on(page) if a.uid == uid)
            self.assertAlmostEqual(after.rect[0], moved[0], delta=2.5)
            self.assertAlmostEqual(after.rect[1], moved[1], delta=2.5)
            self.session.undo()
            back = next(a for a in self.session.annots_on(page) if a.uid == uid)
            self.assertAlmostEqual(back.rect[0], old[0], delta=2.5)
            self.assertAlmostEqual(back.rect[1], old[1], delta=2.5)
        self._assert_source_untouched()

    def test_property_change_and_delete_round_trip(self) -> None:
        page = 0
        uid = self.session.add_shape(page, E.TYPE_RECT, (100, 300, 200, 360),
                                     stroke=(0.0, 0.5, 0.0), width=1.4)
        self.session.set_annot_props(page, uid, stroke=(0.9, 0.2, 0.2), width=3.0)
        with self.session.document._lock:
            _page, annot = self.session._locate(page, uid)
            self.assertAlmostEqual(annot.border["width"], 3.0, places=3)
        self.session.undo()
        with self.session.document._lock:
            _page, annot = self.session._locate(page, uid)
            self.assertAlmostEqual(annot.border["width"], 1.4, places=3)
        before = _render_digest(self.session)
        self.session.delete_annot(page, uid)
        self.assertEqual(self.session.annots_on(page), [])
        self.session.undo()
        self.assertEqual(_render_digest(self.session), before)
        self.assertEqual(self.session.annots_on(page)[0].uid, uid)

    # -- grouping and checkpoints -----------------------------------------
    def test_group_collapses_many_commands_into_one_undo_entry(self) -> None:
        self.session.begin_group("Auto-fill (2 notes)")
        self.session.add_note(0, (300, 100), "one")
        self.session.add_note(0, (320, 120), "two")
        self.session.end_group()
        self.assertEqual(len(self.session.annots_on(0)), 2)
        self.assertEqual(self.session.undo_label, "Auto-fill (2 notes)")
        self.session.undo()
        self.assertEqual(self.session.annots_on(0), [])
        self.assertFalse(self.session.can_undo)

    def test_checkpoint_wraps_external_mutation_and_restores_on_error(self) -> None:
        before = _render_digest(self.session)
        with self.session.checkpoint("Stamp"):
            with self.session.document._lock:
                self.session._page(0).insert_text((72, 200), "stamped", fontsize=9)
        self.assertIn("stamped", self.session.page_text(0))
        self.session.undo()
        self.assertEqual(_render_digest(self.session), before)
        with self.assertRaises(RuntimeError):
            with self.session.checkpoint("Exploding edit"):
                with self.session.document._lock:
                    self.session._page(0).insert_text((72, 220), "leak", fontsize=9)
                raise RuntimeError("boom")
        self.assertNotIn("leak", self.session.page_text(0))
        self.assertEqual(_render_digest(self.session), before)

    # -- redaction ----------------------------------------------------------
    def test_redaction_removes_text_and_undo_restores_it(self) -> None:
        page = 0
        with self.session.document._lock:
            hits = self.session._page(page).search_for("marked up")
        rect = hits[0]
        before = _render_digest(self.session)
        self.session.mark_redaction(page, (rect.x0, rect.y0, rect.x1, rect.y1))
        self.session.apply_redactions()
        self.assertNotIn("marked up", self.session.page_text(page))
        self.session.undo()   # the apply
        self.session.undo()   # the mark
        self.assertIn("marked up", self.session.page_text(page))
        self.assertEqual(_render_digest(self.session), before)
        self._assert_source_untouched()

    # -- page operations -----------------------------------------------------
    def test_page_operations_round_trip_through_undo(self) -> None:
        operations = (
            ("rotate", lambda: self.session.rotate_page(0, 90)),
            ("delete", lambda: self.session.delete_pages([1])),
            ("move", lambda: self.session.move_page(1, 0)),
            ("duplicate", lambda: self.session.duplicate_page(0)),
            ("insert blank", lambda: self.session.insert_blank_page(1)),
        )
        for name, operation in operations:
            with self.subTest(operation=name):
                before = (_render_digest(self.session, 0),
                          self.session.document.page_count)
                operation()
                self.assertTrue(self.session.dirty)
                self.session.undo()
                after = (_render_digest(self.session, 0),
                         self.session.document.page_count)
                self.assertEqual(after, before)
                self.assertFalse(self.session.dirty)
        self._assert_source_untouched()

    def test_deleting_every_page_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.session.delete_pages([0, 1])

    def test_insert_pdf_and_extract_pages(self) -> None:
        other = self.root / "other.pdf"
        doc = D.fitz.open()
        doc.new_page(width=400, height=400).insert_text((50, 50), "inserted page")
        doc.save(str(other))
        doc.close()
        self.session.insert_pdf_file(other, at=1)
        self.assertEqual(self.session.document.page_count, 3)
        self.assertIn("inserted page", self.session.page_text(1))
        out = self.root / "extracted.pdf"
        self.session.extract_pages([1], out)
        extracted = D.fitz.open(str(out))
        self.assertEqual(extracted.page_count, 1)
        self.assertIn("inserted page", extracted[0].get_text())
        extracted.close()
        self.session.undo()
        self.assertEqual(self.session.document.page_count, 2)
        self._assert_source_untouched()

    # -- forms ---------------------------------------------------------------
    def test_form_field_fill_round_trips_and_survives_serialisation(self) -> None:
        form_pdf = self.root / "form.pdf"
        doc = D.fitz.open()
        page = doc.new_page(width=595, height=842)
        widget = D.fitz.Widget()
        widget.field_name = "applicant_name"
        widget.field_type = D.fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = D.fitz.Rect(72, 300, 300, 320)
        widget.field_value = ""
        page.add_widget(widget)
        doc.save(str(form_pdf))
        doc.close()
        session = E.EditSession(_entry(form_pdf))
        self.addCleanup(session.close)
        with session.document._lock:
            xref = next(iter(session._page(0).widgets())).xref
        session.set_field_value(0, xref, "Andrianarivelo")
        with session.document._lock:
            _page, widget = session._locate_widget(0, xref)
            self.assertEqual(widget.field_value, "Andrianarivelo")
        reopened = D.fitz.open(stream=session.save_bytes(), filetype="pdf")
        self.assertEqual(next(iter(reopened[0].widgets())).field_value,
                         "Andrianarivelo")
        reopened.close()
        session.undo()
        with session.document._lock:
            _page, widget = session._locate_widget(0, xref)
            self.assertEqual(widget.field_value, "")

    # -- history budget -------------------------------------------------------
    def test_budget_eviction_truncates_history_and_pins_dirty(self) -> None:
        import unittest.mock as mock
        with mock.patch.object(E, "MAX_COMMANDS", 5):
            self.session.add_note(0, (300, 100), "anchor")
            self.session.mark_saved(_entry(self.pdf))
            for index in range(6):
                self.session.add_note(0, (300, 120 + 10 * index), f"n{index}")
            self.assertTrue(self.session.history_truncated)
            self.assertLessEqual(len(self.session._history.undo), 5)
            while self.session.can_undo:
                self.session.undo()
            self.assertTrue(
                self.session.dirty,
                "after losing the save marker, no undo depth may claim clean",
            )

    def test_save_bytes_reflects_edits_without_touching_disk(self) -> None:
        self.session.add_note(0, (300, 100), "will be saved")
        data = self.session.save_bytes()
        reopened = D.fitz.open(stream=data, filetype="pdf")
        annot_types = [a.type[0] for a in reopened[0].annots()]
        reopened.close()
        self.assertEqual(annot_types, [D.fitz.PDF_ANNOT_TEXT])
        self._assert_source_untouched()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for cache tests")
class EditableDocumentCacheTests(unittest.TestCase):
    """The docs.py surgery: eviction, tile versioning, mutation under load."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "cache.pdf"
        doc = D.fitz.open()
        doc.new_page(width=595, height=842).insert_text((72, 100), "cache fixture")
        doc.save(str(self.pdf))
        doc.close()

    def test_cache_evict_drops_only_matching_paths(self) -> None:
        other = self.root / "other.pdf"
        doc = D.fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(str(other))
        doc.close()
        cache = D.DocumentCache(max_items=6)
        kept = cache.get(_entry(other))
        cache.get(_entry(self.pdf))
        self.assertEqual(cache.evict(self.pdf), 1)
        self.assertIsNone(cache.peek(_entry(self.pdf)))
        self.assertIs(cache.peek(_entry(other)), kept)
        cache.clear()

    def test_stale_tile_is_never_served_after_a_content_bump(self) -> None:
        session = E.EditSession(_entry(self.pdf))
        self.addCleanup(session.close)
        document = session.document
        width, height = document.page_size(0)
        first = document.render_region(0, (0, 0, width, height), (200, 283))
        self.assertIsNotNone(document._tile)
        session.add_shape(0, E.TYPE_RECT, (100, 100, 400, 400),
                          stroke=(1.0, 0.0, 0.0), width=8.0)
        self.assertIsNone(document._tile, "mutation must drop the padded tile")
        second = document.render_region(0, (0, 0, width, height), (200, 283))
        self.assertNotEqual(first.tobytes(), second.tobytes(),
                            "the re-render must show the new annotation")

    def test_concurrent_render_while_mutating_does_not_crash(self) -> None:
        import threading
        session = E.EditSession(_entry(self.pdf))
        self.addCleanup(session.close)
        document = session.document
        width, height = document.page_size(0)
        errors: list[BaseException] = []
        stop = threading.Event()

        def render_loop() -> None:
            while not stop.is_set():
                try:
                    document.render_region(0, (0, 0, width, height), (120, 170))
                    document.page_size(0)
                except BaseException as exc:  # noqa: BLE001 - collected for assert
                    errors.append(exc)
                    return

        thread = threading.Thread(target=render_loop, daemon=True)
        thread.start()
        try:
            for index in range(60):
                session.add_note(0, (200 + index, 200), f"n{index}")
                if index % 7 == 0:
                    session.undo()
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
