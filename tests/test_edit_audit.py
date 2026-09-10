"""Regression tests for the 2026-09-10 editing audit.

Each case reproduces one confirmed defect of the edit stack and pins the
fix: history eviction hiding unsaved edits, half-failed snapshot mutations,
unnamed foreign annotations sharing one identity, redaction leaving image
pixels and vector art behind, text markups pretending to move, change
events raised from worker threads, an in-flight save marking later edits as
saved, thumb-strip reorder arithmetic, rotated free text, and (live) a
session leaking from one document onto the next through Ctrl+S.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from figureviewer import docs as D
from figureviewer import editui
from figureviewer import pdfedit as PE


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _make_pdf(path: Path, text: str = "Audit fixture", *, pages: int = 2,
              rotation: int = 0, with_image: bool = False) -> None:
    import pymupdf

    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=400, height=300)
        page.insert_text((40, 60), f"{text} {number + 1}", fontsize=18)
        if with_image:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
            pix.clear_with(200)
            page.insert_image(pymupdf.Rect(40, 100, 200, 260), pixmap=pix)
            shape = page.new_shape()
            shape.draw_rect(pymupdf.Rect(220, 100, 300, 180))
            shape.finish(fill=(1, 0, 0), color=None)
            shape.commit()
        if rotation:
            page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class SessionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "doc.pdf"
        _make_pdf(self.pdf, with_image=True)
        self.session = PE.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)
        self.addCleanup(self._tmp.cleanup)

    # -- history -----------------------------------------------------------
    def test_evicting_the_first_edit_of_a_never_saved_session_keeps_dirty(self):
        with mock.patch.object(PE, "MAX_SNAPSHOTS", 2):
            for _ in range(3):
                self.session.rotate_page(0, 90)
            self.assertTrue(self.session.history_truncated)
            while self.session.can_undo:
                self.session.undo()
        # The document is still rotated once relative to the disk file.
        with self.session.document._lock:
            self.assertEqual(self.session.document._page(0).rotation, 90)
        self.assertTrue(self.session.dirty,
                        "an unrecoverable edit must keep the session dirty")

    def test_failed_snapshot_mutation_restores_and_stays_clean(self):
        broken = self.root / "broken.pdf"
        broken.write_bytes(b"not a pdf")
        good = self.root / "good.pdf"
        _make_pdf(good, "Extra", pages=1)
        before = self.session.document.to_bytes()
        with self.assertRaises(Exception):
            self.session.merge_append([good, broken])
        self.assertEqual(self.session.document.page_count, 2,
                         "the half-applied merge must be rolled back")
        self.assertFalse(self.session.dirty)
        self.assertFalse(self.session.can_undo)
        self.assertEqual(len(self.session.document.to_bytes()), len(before))

    # -- identity ----------------------------------------------------------
    def test_unnamed_foreign_annotations_keep_distinct_identities(self):
        with self.session.document._lock:
            page = self.session.document._doc[0]
            page.add_rect_annot(D.fitz.Rect(10, 10, 50, 50)).update()
            page.add_rect_annot(D.fitz.Rect(100, 100, 150, 150)).update()
            del page
        infos = self.session.annots_on(0)
        self.assertEqual(len(infos), 2)
        self.assertNotEqual(infos[0].uid, infos[1].uid)
        second = infos[1]
        self.session.delete_annot(0, second.uid)
        remaining = self.session.annots_on(0)
        self.assertEqual(len(remaining), 1)
        self.assertAlmostEqual(remaining[0].rect[0], 10.0, delta=1.5,
                               msg="the FIRST rectangle must survive")

    # -- redaction ---------------------------------------------------------
    def test_apply_redactions_removes_covered_pixels_and_vector_art(self):
        fitz = D.fitz
        self.session.mark_redaction(0, (40, 100, 120, 260))     # half the image
        self.session.mark_redaction(1, (0, 0, 400, 300))        # whole page 2
        self.assertEqual(self.session.pending_redactions(), 2)
        self.session.mark_redaction(0, (215, 95, 305, 185))     # the red box
        self.session.apply_redactions()
        data = self.session.document.to_bytes()
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            page = doc[0]
            pix = page.get_pixmap(clip=fitz.Rect(40, 100, 120, 260), alpha=False)
            left = [pix.pixel(x, y) for x in range(0, pix.width, 8)
                    for y in range(0, pix.height, 8)]
            self.assertTrue(all(p == (255, 255, 255) or p == (0, 0, 0) for p in left),
                            "covered image pixels must be blanked or boxed")
            pix = page.get_pixmap(clip=fitz.Rect(150, 100, 200, 260), alpha=False)
            self.assertIn((200, 200, 200), {pix.pixel(10, 10)},
                          "the uncovered part of the image survives")
            drawings = page.get_drawings()
            self.assertFalse(any(d.get("fill") == (1.0, 0.0, 0.0) for d in drawings),
                             "a fully covered vector shape must be removed")
            self.assertEqual(doc[1].get_text("text").strip(), "")
        finally:
            doc.close()

    # -- markups -----------------------------------------------------------
    def test_text_markups_refuse_to_move_without_polluting_history(self):
        with self.session.document._lock:
            words = self.session.document._doc[0].get_text("words")
        quad = D.fitz.Rect(words[0][:4]).quad
        uid = self.session.add_markup(0, [quad], PE.TYPE_HIGHLIGHT,
                                      (1.0, 0.9, 0.2), 0.5)
        depth = len(self.session._history.undo)
        with self.assertRaises(ValueError):
            self.session.move_annot(0, uid, (200, 200, 260, 220))
        self.assertEqual(len(self.session._history.undo), depth)

    def test_resizing_ink_keeps_opacity_and_comment(self):
        uid = self.session.add_ink(0, [[(10, 10), (60, 40), (90, 20)]],
                                   (0.1, 0.2, 0.8), 2.0)
        self.session.set_annot_props(0, uid, opacity=0.4, contents="a note")
        self.session.move_annot(0, uid, (100, 100, 180, 140))
        info = [a for a in self.session.annots_on(0) if a.uid == uid][0]
        with self.session.document._lock:
            _page, annot = self.session._locate(0, uid)
            self.assertAlmostEqual(annot.opacity, 0.4, places=2)
            self.assertEqual((annot.info or {}).get("content"), "a note")
            del annot, _page
        self.assertAlmostEqual(info.rect[0], 100.0, delta=2.0)

    # -- threading ---------------------------------------------------------
    def test_change_events_from_a_worker_are_marshalled(self):
        seen: list[tuple[str, str]] = []
        dispatched: list = []
        self.session.ui_dispatch = dispatched.append
        self.session.on_change.append(
            lambda event: seen.append((threading.current_thread().name, event.label)))
        self.session.rotate_page(0, 90)
        self.assertEqual(seen[-1][1], "Rotate page")
        self.assertEqual(dispatched, [], "on-thread events are delivered directly")

        def worker() -> None:
            with self.session.checkpoint("Worker edit"):
                with self.session.document._lock:
                    self.session.document._doc[0].insert_text((10, 200), "x")

        thread = threading.Thread(target=worker, name="WORKER")
        thread.start()
        thread.join()
        self.assertEqual(len(dispatched), 1, "the worker's event waits for the UI")
        self.assertFalse(any(name == "WORKER" for name, _ in seen))
        dispatched[0]()
        self.assertEqual(seen[-1], (threading.current_thread().name, "Worker edit"))

    # -- save marker -------------------------------------------------------
    def test_edit_during_an_in_flight_save_stays_dirty(self):
        self.session.add_note(0, (50, 50), "first")
        data = self.session.save_bytes()
        top = self.session.history_top()
        self.session.add_note(0, (80, 80), "second, while writing")
        self.pdf.write_bytes(data)
        self.session.mark_saved(_entry(self.pdf), top)
        self.assertTrue(self.session.dirty)
        self.session.undo()
        self.assertFalse(self.session.dirty)

    # -- rotation ----------------------------------------------------------
    def test_free_text_on_a_rotated_page_carries_the_page_rotation(self):
        rotated = self.root / "rotated.pdf"
        _make_pdf(rotated, rotation=90, pages=1)
        session = PE.EditSession(_entry(rotated))
        self.addCleanup(session.close)
        uid = session.add_free_text(0, (20, 20, 120, 60), "Upright")
        with session.document._lock:
            _page, annot = session._locate(0, uid)
            rotate = session.document._doc.xref_get_key(annot.xref, "Rotate")
            del annot, _page
        self.assertEqual(rotate[1], "90")


class ReorderPlanTests(unittest.TestCase):
    def test_one_slot_to_the_right_is_a_real_move(self) -> None:
        # Page 0 dropped before slot 2 ends up at index 1.
        self.assertEqual(editui.reorder_plan(0, 2, 4), (2, 1))

    def test_drops_that_change_nothing_are_skipped(self) -> None:
        self.assertIsNone(editui.reorder_plan(1, 1, 4))
        self.assertIsNone(editui.reorder_plan(1, 2, 4))

    def test_leftward_and_end_drops(self) -> None:
        self.assertEqual(editui.reorder_plan(3, 0, 4), (0, 0))
        self.assertEqual(editui.reorder_plan(0, 4, 4), (-1, 3))


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class SessionLeakLiveTests(unittest.TestCase):
    """Editing A then opening B must never let Ctrl+S write A over B."""

    def _start(self, stack: ExitStack, root: Path, target: Path):
        from figureviewer import app as app_module

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

    def test_session_is_parked_when_another_document_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a_path, b_path = root / "a.pdf", root / "b.pdf"
            _make_pdf(a_path, "DOCUMENT A")
            _make_pdf(b_path, "DOCUMENT B")
            b_before = b_path.read_bytes()
            with ExitStack() as stack:
                application = self._start(stack, root, a_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    window.edit.activate()
                    window.edit.session.add_note(0, (50, 50), "edit on A")
                    self.assertTrue(window.edit.dirty)

                    window.add_files([b_path])
                    self._wait(application, lambda: window.current is not None
                               and window.current.path == b_path
                               and window.document is not None
                               and getattr(window.document, "entry", None) is not None
                               and window.document.entry.path == b_path)
                    application.root.update()
                    self.assertIsNone(window.edit.session,
                                      "A's session must not follow the window to B")
                    self.assertFalse(window.edit.active)
                    self.assertFalse(window.edit.dirty)
                    self.assertIsNotNone(application.peek_session(a_path),
                                         "A's edits stay parked")

                    # Ctrl+S on B has nothing to save and never touches B.
                    self.assertFalse(window.edit.save(synchronous=True))
                    self.assertEqual(b_path.read_bytes(), b_before)

                    # Going back to A re-adopts the dirty session.
                    window.add_files([a_path])
                    self._wait(application, lambda: window.edit.session is not None
                               and window.edit.session.entry.path == a_path)
                    self.assertTrue(window.edit.dirty)
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
