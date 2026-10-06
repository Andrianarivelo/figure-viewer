"""PDF review metadata survives history and interoperable PDF serialization."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_reply_native_thread_metadata_and_history(self) -> None:
        """Thread references, Unicode and dates survive fresh xrefs and saves."""
        root = self.session.add_note(0, (300, 100), "Root comment")
        with self.session.document._lock:
            pixels = self.session._page(0).get_pixmap().samples
        contents = "Discussion \u03b1 \u4e2d\u6587 \U0001f9e0\nSecond line"
        author = "Zo\u00eb \u4e2d\u6587 \U0001f9e0"
        reply = self.session.add_reply(0, root, contents, author)
        info = self.session.annots_on(0)[1]
        self.assertEqual((info.uid, info.parent_uid, info.contents, info.author),
                         (reply, root, contents, author))
        self.assertTrue(info.created.startswith("D:"))
        with self.session.document._lock:
            self.assertEqual(self.session._page(0).get_pixmap().samples, pixels)
        selected = self.session.hit_test(0, (305, 105))
        self.assertEqual(selected.uid, root)
        for _ in range(2):
            with D.fitz.open(stream=self.session.save_bytes(), filetype="pdf") as saved:
                page = saved[0]
                annots = list(page.annots())
                parent = next(a for a in annots if a.info["id"] == root)
                child = next(a for a in annots if a.info["id"] == reply)
                self.assertEqual(child.irt_xref, parent.xref)
                self.assertEqual(saved.xref_get_key(child.xref, "RT"), ("name", "/R"))
                self.assertEqual(child.info["title"], author)
                self.assertEqual(child.info["content"], contents)
                self.assertEqual(child.info["creationDate"], info.created)
                self.assertTrue(child.flags & D.fitz.PDF_ANNOT_IS_NO_VIEW)
                self.assertFalse(child.flags & D.fitz.PDF_ANNOT_IS_HIDDEN)
                self.assertFalse(child.flags & D.fitz.PDF_ANNOT_IS_PRINT)
                self.session.document.swap_doc(saved.tobytes())
            self.assertEqual(self.session.annots_on(0)[1].parent_uid, root)
            self.session.undo()
            self.assertEqual([a.uid for a in self.session.annots_on(0)], [root])
            self.session.undo()
            self.assertEqual(self.session.annots_on(0), [])
            self.session.redo()
            self.session.redo()
            restored = self.session.annots_on(0)[1]
            self.assertEqual((restored.uid, restored.parent_uid, restored.contents,
                              restored.author), (reply, root, contents, author))
            self.assertEqual(self.session.annots_on(0)[1].created, info.created)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_imported_unnamed_parent_and_native_irt(self) -> None:
        """Foreign replies default to /R and resolve parents lacking /NM."""
        with self.session.document._lock:
            page = self.session._page(0)
            root = page.add_text_annot((300, 100), "Imported parent")
            child = page.add_text_annot((300, 100), "Imported reply")
            document = self.session.document._doc
            document.xref_set_key(root.xref, "NM", "null")
            document.xref_set_key(child.xref, "NM", "null")
            document.xref_set_key(child.xref, "IRT", f"{root.xref} 0 R")
            root_uid = f"xref:{root.xref}"
        infos = self.session.annots_on(0)
        self.assertEqual(infos[0].uid, root_uid)
        self.assertEqual(infos[1].parent_uid, root_uid)
        uid = self.session.add_reply(0, root_uid, "New reply")
        self.assertEqual(self.session.annots_on(0)[2].parent_uid, root_uid)
        self.session.undo()
        self.session.redo()
        self.assertEqual(self.session.annots_on(0)[2].uid, uid)
        self.session.document.swap_doc(self.session.save_bytes())
        infos = self.session.annots_on(0)
        self.assertEqual(infos[1].parent_uid, infos[0].uid)
        self.assertEqual(infos[2].parent_uid, infos[0].uid)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_thread_deletion_is_atomic_and_preserves_sibling_threads(self) -> None:
        """Deleting replies or roots removes descendants with one undo step."""
        root = self.session.add_note(0, (300, 100), "Root")
        reply = self.session.add_reply(0, root, "Reply")
        nested = self.session.add_reply(0, reply, "Nested reply")
        sibling = self.session.add_reply(0, root, "Sibling")
        other = self.session.add_note(0, (400, 100), "Independent")
        events = []
        self.session.on_change.append(events.append)
        self.session.delete_annot(0, reply)
        self.assertEqual(len(events), 1)
        self.assertEqual({a.uid for a in self.session.annots_on(0)},
                         {root, sibling, other})
        self.session.undo()
        self.assertEqual({a.uid for a in self.session.annots_on(0)},
                         {root, reply, nested, sibling, other})
        self.session.redo()
        self.session.undo()
        self.session.delete_annot(0, root)
        self.assertEqual([a.uid for a in self.session.annots_on(0)], [other])
        self.session.undo()
        self.assertEqual({a.uid for a in self.session.annots_on(0)},
                         {root, reply, nested, sibling, other})
        self.session.redo()
        self.assertEqual([a.uid for a in self.session.annots_on(0)], [other])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_group_relationship_is_not_a_reply(self) -> None:
        """PDF grouped markup stays visible as an independent annotation."""
        root = self.session.add_note(0, (300, 100), "Group leader")
        grouped = self.session.add_note(0, (400, 100), "Grouped markup")
        with self.session.document._lock:
            page, leader = self.session._locate(0, root)
            leader_xref = leader.xref
            page, member = self.session._locate(0, grouped)
            member.set_irt_xref(leader_xref)
            self.session.document._doc.xref_set_key(member.xref, "RT", "/Group")
        infos = {a.uid: a for a in self.session.annots_on(0)}
        self.assertEqual(infos[grouped].parent_uid, "")
        self.assertEqual(self.session.hit_test(0, (405, 105)).uid, grouped)

    def test_long_thread_enumerates_page_xrefs_once(self) -> None:
        """Parent lookup shares one page index rather than rescanning replies."""
        root = self.session.add_note(0, (300, 100), "Root")
        for index in range(40):
            self.session.add_reply(0, root, f"Reply {index}")
        original = D.fitz.Page.annot_xrefs
        calls = []

        def counted(page):
            """Count native enumeration calls without replacing its result."""
            calls.append(page.number)
            return original(page)

        with patch.object(D.fitz.Page, "annot_xrefs", counted):
            infos = self.session.annots_on(0)
        self.assertEqual(len(infos), 41)
        self.assertTrue(all(info.parent_uid == root for info in infos[1:]))
        self.assertEqual(calls, [0])

    def test_malformed_imported_parent_names_and_relationships(self) -> None:
        """Invalid PDF names use xref identity and broken links stay readable."""
        root = self.session.add_note(0, (300, 100), "Imported root")
        reply = self.session.add_reply(0, root, "Imported reply")
        with self.session.document._lock:
            page, parent = self.session._locate(0, root)
            parent_xref = parent.xref
            page, child = self.session._locate(0, reply)
            child_xref = child.xref
        document = self.session.document._doc
        for malformed in ("/InvalidName", "42", "()", "null"):
            with self.subTest(name=malformed), self.session.document._lock:
                document.xref_set_key(parent_xref, "NM", malformed)
                infos = self.session.annots_on(0)
                self.assertEqual(infos[0].uid, f"xref:{parent_xref}")
                self.assertEqual(infos[1].parent_uid, infos[0].uid)
        for malformed in ("/InvalidReference", "42", "null", f"{child_xref} 0 R"):
            with self.subTest(reference=malformed), self.session.document._lock:
                document.xref_set_key(child_xref, "IRT", malformed)
                self.assertEqual(self.session.annots_on(0)[1].parent_uid, "")

    def test_reply_rejects_missing_parent_and_blank_contents_without_changes(self) -> None:
        """Failed validation leaves the document, history and raw file intact."""
        root = self.session.add_note(0, (300, 100), "Root")
        before = self.session.annots_on(0)
        version = self.session.content_version
        history = self.session.history_top()
        for contents in ("", " \n\t ", None):
            with self.assertRaises(ValueError):
                self.session.add_reply(0, root, contents)
        with self.assertRaises(LookupError):
            self.session.add_reply(0, "missing", "Valid contents")
        self.assertEqual(self.session.annots_on(0), before)
        self.assertEqual(self.session.content_version, version)
        self.assertIs(self.session.history_top(), history)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_moving_geometry_parent_keeps_reply_thread(self) -> None:
        """Recreating line and ink geometry must retain linked messages."""
        for kind in (E.TYPE_LINE, E.TYPE_INK):
            with self.subTest(kind=kind):
                if kind == E.TYPE_INK:
                    root = self.session.add_ink(0, [[(300, 100), (340, 110)]],
                                                (0.2, 0.2, 0.2), 1.0)
                else:
                    root = self.session.add_shape(0, kind, ((300, 100), (340, 110)),
                                                  stroke=(0.2, 0.2, 0.2))
                reply = self.session.add_reply(0, root, "Keep discussion")
                nested = self.session.add_reply(0, reply, "Keep nested reply")
                self.session.move_annot(0, root, (320, 120, 370, 135))
                for cycle in range(3):
                    infos = {a.uid: a for a in self.session.annots_on(0)}
                    self.assertEqual(infos[reply].parent_uid, root)
                    self.assertEqual(infos[nested].parent_uid, reply)
                    self.assertEqual(infos[reply].contents, "Keep discussion")
                    if cycle == 0:
                        self.session.undo()
                    elif cycle == 1:
                        self.session.redo()
                self.session.delete_annot(0, root)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_empty_comment_edit_and_undo_survive_save_and_redo(self) -> None:
        """Clearing /Contents must work both explicitly and when undoing an edit."""
        comment = "Révision α 中文 🧠\nDeuxième ligne"
        subject = "Original selected text"
        uid = self.session.add_markup(0, self.quads, E.TYPE_HIGHLIGHT,
                                      (1, 0.8, 0.2), 0.35, subject=subject)

        def assert_comment(expected):
            """Check live metadata, serialized metadata and untouched geometry."""
            info, = self.session.annots_on(0)
            self.assertEqual(info.contents, expected)
            self.assertEqual(info.subject, subject)
            self.assertEqual(info.uid, uid)
            with D.fitz.open(stream=self.session.save_bytes(), filetype="pdf") as saved:
                page = saved[0]
                annot, = list(page.annots())
                self.assertEqual(annot.info["content"], expected)
                self.assertEqual(annot.info["subject"], subject)
                self.assertEqual(len(annot.vertices), 8)
            self.assertEqual(self.path.read_bytes(), self.original)

        self.session.set_annot_props(0, uid, contents=comment)
        assert_comment(comment)
        self.session.undo()
        assert_comment("")
        self.session.redo()
        assert_comment(comment)
        self.session.set_annot_props(0, uid, contents="")
        assert_comment("")
        self.session.undo()
        assert_comment(comment)
        self.session.redo()
        assert_comment("")


if __name__ == "__main__":
    unittest.main()
