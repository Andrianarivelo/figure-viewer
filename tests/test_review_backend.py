"""PDF review metadata survives history and interoperable PDF serialization."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from figureviewer import docs as D
from figureviewer import pdfedit as E


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for review tests")
class ReviewBackendTests(unittest.TestCase):
    """Use only generated fixtures and verify the source bytes stay intact."""

    def setUp(self) -> None:
        """Create selectable text on two lines for multi-quad review markup."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "review.pdf"
        with D.fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 100), "First selected sentence")
            page.insert_text((72, 120), "Second selected sentence")
            document.save(self.path)
        self.original = self.path.read_bytes()
        stat = self.path.stat()
        self.session = E.EditSession(
            D.Entry(self.path, stat.st_size, stat.st_mtime, D.KIND_DOC))
        self.addCleanup(self.session.close)
        with self.session.document._lock:
            self.quads = self.session._page(0).search_for("selected sentence", quads=True)
        self.assertEqual(len(self.quads), 2)

    def test_all_markup_comments_round_trip_in_one_history_step(self) -> None:
        """Unicode metadata, stable ids and both quads survive save and redo."""
        contents = "À vérifier : α = 0,05.\n中文, Ελληνικά, 🧠 (note) \\ détail"
        subject = "Citation : Résultats α, 中文 🧠"
        events = []
        self.session.on_change.append(events.append)
        for kind in E.MARKUP_KINDS:
            with self.subTest(kind=kind):
                events.clear()
                uid = self.session.add_markup(
                    0, self.quads, kind, (1.0, 0.8, 0.2), 0.35,
                    contents=contents, subject=subject)
                self.assertEqual(len(events), 1)
                info, = self.session.annots_on(0)
                self.assertEqual((info.uid, info.contents, info.subject),
                                 (uid, contents, subject))
                for cycle in range(2):
                    with D.fitz.open(stream=self.session.save_bytes(), filetype="pdf") as saved:
                        page = saved[0]
                        annot, = list(page.annots())
                        self.assertEqual(annot.info["content"], contents)
                        self.assertEqual(annot.info["subject"], subject)
                        self.assertEqual(annot.info["id"], uid)
                        self.assertEqual(len(annot.vertices), 8)
                        self.assertAlmostEqual(annot.opacity, 0.35, places=5)
                    self.session.undo()
                    self.assertFalse(self.session.can_undo)
                    self.assertFalse(self.session.dirty)
                    self.assertEqual(self.session.annots_on(0), [])
                    if cycle == 0:
                        self.session.redo()
                        restored, = self.session.annots_on(0)
                        self.assertEqual(restored.uid, uid)
                        self.assertEqual(restored.kind, kind)
                        self.assertEqual(restored.contents, contents)
                        self.assertEqual(restored.subject, subject)
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_optional_metadata_preserves_existing_callers(self) -> None:
        """The old positional signature and independent optional fields work."""
        for metadata in ({}, {"contents": "Commentaire"}, {"subject": "Citation"}):
            with self.subTest(metadata=metadata):
                self.session.add_markup(0, self.quads, E.TYPE_HIGHLIGHT,
                                        (1.0, 1.0, 0.0), 0.4, **metadata)
                info, = self.session.annots_on(0)
                self.assertEqual(info.contents, metadata.get("contents", ""))
                self.assertEqual(info.subject, metadata.get("subject", ""))
                self.session.undo()
        legacy = E.AnnotInfo(0, 1, "uid", E.TYPE_NOTE, (0, 0, 1, 1), "note")
        self.assertEqual(legacy.subject, "")


if __name__ == "__main__":
    unittest.main()
