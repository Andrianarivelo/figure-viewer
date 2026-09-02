"""Signature tests: library CRUD, stamping into a session, crypto signing.

Everything runs headless in temporary directories; every fixture (PDFs,
signature scans, certificates) is generated on the fly and no user file is
ever read or written. The suite pins the three store invariants: the PNG is
written under ``store_dir`` BEFORE the metadata claims it, the ``save``
callback fires on every mutation, and the load-time sweep drops both dead
metadata and orphan store-named PNGs. The stamping test borrows the render
digest from ``tests.test_pdfedit``: undo must restore the exact rendered
document, not merely the byte count.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

from figureviewer import docs as D
from figureviewer import pdfedit as E
from figureviewer import signing as S
from tests.test_pdfedit import _render_digest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


#: Two plausible pen strokes in an arbitrary capture coordinate space.
STROKES = [
    [(12.0, 48.0), (40.0, 12.0), (90.0, 52.0), (150.0, 18.0), (200.0, 40.0)],
    [(30.0, 64.0), (180.0, 64.0)],
]


class RecordingSave:
    """A save callback that counts calls and audits the PNG-first invariant.

    At every invocation, every metadata row must already have its PNG on
    disk; a row observed without its file is recorded as a violation.
    """

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = Path(store_dir)
        self.calls = 0
        self.violations: list[str] = []

    def __call__(self, settings: dict) -> None:
        self.calls += 1
        for item in settings.get("signature_library", []):
            if not (self.store_dir / str(item.get("file", ""))).is_file():
                self.violations.append(str(item.get("file", "")))


class SignatureLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = self.root / "signatures"
        self.settings: dict = {}
        self.save = RecordingSave(self.store)
        self.library = S.SignatureLibrary(self.settings, self.save, self.store)

    # -- capture routes ----------------------------------------------------
    def test_add_drawn_stores_a_decodable_transparent_png(self) -> None:
        uid = self.library.add_drawn(STROKES, color="#1a1a1a", width=3.0)
        self.assertEqual(self.save.calls, 1, "save must fire on the mutation")
        self.assertEqual(self.save.violations, [],
                         "metadata must never claim a PNG that is not on disk")
        items = self.library.items()
        self.assertEqual([item["id"] for item in items], [uid])
        self.assertEqual(items[0]["kind"], S.KIND_DRAWN)
        self.assertTrue((self.store / items[0]["file"]).is_file())
        data = self.library.png_bytes(uid)
        self.assertTrue(data.startswith(b"\x89PNG"))
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.mode, "RGBA")
        alpha = image.getchannel("A")
        self.assertIsNotNone(alpha.getbbox(), "the ink must be opaque")
        low, high = alpha.getextrema()
        self.assertEqual(low, 0, "the background must stay transparent")
        self.assertEqual(high, 255)

    def test_add_typed_renders_the_text_and_labels_the_item(self) -> None:
        uid = self.library.add_typed("Andry Rakoto")
        meta = self.library.item(uid)
        self.assertEqual(meta["kind"], S.KIND_TYPED)
        self.assertEqual(meta["label"], "Andry Rakoto")
        image = Image.open(io.BytesIO(self.library.png_bytes(uid)))
        self.assertIsNotNone(image.getchannel("A").getbbox())
        self.assertGreater(image.width, image.height,
                           "a typed name renders wider than tall")

    def test_add_image_removes_white_background_without_touching_source(
            self) -> None:
        scan = self.root / "scan.png"
        image = Image.new("RGB", (160, 60), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.line([(10, 40), (60, 15), (120, 45), (150, 20)],
                  fill=(20, 20, 90), width=4)
        image.save(scan)
        source_hash = _sha(scan)
        uid = self.library.add_image(scan)
        self.assertEqual(_sha(scan), source_hash,
                         "the raw user file must never change")
        stored = Image.open(io.BytesIO(self.library.png_bytes(uid)))
        alpha = stored.getchannel("A")
        low, high = alpha.getextrema()
        self.assertEqual(low, 0, "near-white paper must become transparent")
        self.assertEqual(high, 255, "the ink itself must stay opaque")
        self.assertEqual(self.library.item(uid)["kind"], S.KIND_IMAGE)

    def test_add_image_accepts_bytes_and_can_keep_the_background(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), (255, 255, 255)).save(buffer, "PNG")
        uid = self.library.add_image(buffer.getvalue(), remove_background=False)
        stored = Image.open(io.BytesIO(self.library.png_bytes(uid)))
        self.assertEqual(stored.getchannel("A").getextrema(), (255, 255),
                         "remove_background=False must keep every pixel")

    # -- CRUD and persistence ---------------------------------------------
    def test_rename_and_remove_update_disk_first_and_save_each_time(
            self) -> None:
        uid = self.library.add_typed("Temp name")
        png_path = self.store / self.library.item(uid)["file"]
        self.assertTrue(png_path.is_file())
        calls_after_add = self.save.calls
        self.library.rename(uid, "Better label")
        self.assertEqual(self.library.item(uid)["label"], "Better label")
        self.assertEqual(self.save.calls, calls_after_add + 1)
        self.library.remove(uid)
        self.assertFalse(png_path.exists(), "remove must delete the PNG")
        self.assertEqual(self.library.items(), [])
        self.assertEqual(self.save.calls, calls_after_add + 2)
        self.assertEqual(self.save.violations, [])
        with self.assertRaises(KeyError):
            self.library.png_bytes(uid)

    def test_png_write_failure_leaves_metadata_and_save_untouched(self) -> None:
        blocked = self.root / "blocked-store"
        blocked.write_text("a FILE where the store directory should be")
        settings: dict = {}
        save = RecordingSave(blocked)
        library = S.SignatureLibrary(settings, save, blocked)
        with self.assertRaises(OSError):
            library.add_typed("never stored")
        self.assertEqual(settings["signature_library"], [],
                         "no PNG on disk means no metadata, ever")
        self.assertEqual(save.calls, 0)

    def test_empty_captures_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.library.add_drawn([])
        with self.assertRaises(ValueError):
            self.library.add_typed("   ")
        with self.assertRaises(S.SigningError):
            self.library.add_image(b"definitely not an image")
        self.assertEqual(self.library.items(), [])
        self.assertEqual(self.save.calls, 0)

    def test_thumbnail_preserves_aspect_ratio(self) -> None:
        uid = self.library.add_drawn(STROKES)
        image = Image.open(io.BytesIO(self.library.png_bytes(uid)))
        thumb = self.library.thumbnail(uid, 32)
        self.assertEqual(thumb.height, 32)
        self.assertAlmostEqual(thumb.width / thumb.height,
                               image.width / image.height, delta=0.15)

    # -- the load-time sweep ----------------------------------------------
    def test_load_sweep_drops_dead_metadata_and_orphan_pngs(self) -> None:
        kept_uid = self.library.add_typed("Keeper")
        lost_uid = self.library.add_typed("Loser")
        (self.store / self.library.item(lost_uid)["file"]).unlink()
        orphan = self.store / f"{uuid.uuid4().hex}.png"
        orphan.write_bytes(b"\x89PNG orphan blob")
        foreign = self.store / "users-own-scan.png"
        foreign.write_bytes(b"\x89PNG not ours to delete")
        save2 = RecordingSave(self.store)
        library2 = S.SignatureLibrary(self.settings, save2, self.store)
        self.assertEqual([item["id"] for item in library2.items()], [kept_uid],
                         "metadata without its PNG must be dropped at load")
        self.assertFalse(orphan.exists(),
                         "an unreferenced store-named PNG must be deleted")
        self.assertTrue(foreign.exists(),
                        "files outside the uid naming scheme are never touched")
        self.assertGreaterEqual(save2.calls, 1,
                                "the pruned metadata must be persisted")
        self.assertEqual(save2.violations, [])
        self.assertTrue(
            (self.store / library2.item(kept_uid)["file"]).is_file())

    def test_clean_reload_neither_saves_nor_deletes(self) -> None:
        uid = self.library.add_drawn(STROKES)
        save2 = RecordingSave(self.store)
        library2 = S.SignatureLibrary(self.settings, save2, self.store)
        self.assertEqual([item["id"] for item in library2.items()], [uid])
        self.assertEqual(save2.calls, 0,
                         "an unchanged library must not rewrite settings")


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for stamping tests")
class StampSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "contract.pdf"
        doc = D.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "FigView signing fixture", fontsize=16)
        page.insert_text((72, 700), "Signed: ____________________", fontsize=11)
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)
        library = S.SignatureLibrary({}, lambda settings: None,
                                     self.root / "signatures")
        self.png = library.png_bytes(library.add_drawn(STROKES, width=4.0))

    def test_stamp_changes_the_render_and_one_undo_restores_it(self) -> None:
        before = _render_digest(self.session)
        S.stamp_signature(self.session, 0, (150.0, 640.0, 350.0, 690.0),
                          self.png)
        self.assertTrue(self.session.dirty)
        self.assertEqual(self.session.undo_label, "Sign")
        stamped = _render_digest(self.session)
        self.assertNotEqual(stamped, before,
                            "the signature must change the rendered pixels")
        self.assertEqual(self.session.undo(), "Sign")
        self.assertEqual(_render_digest(self.session), before,
                         "one undo must restore the prior render exactly")
        self.assertFalse(self.session.dirty)
        # Redo replays the after-snapshot. That render may differ from the
        # live post-stamp render by a serialisation hair (PyMuPDF re-encodes
        # the embedded image stream), so what is asserted is the kernel's
        # actual guarantee: the signature is back, and the replay is
        # deterministic across undo/redo cycles.
        self.session.redo()
        redone = _render_digest(self.session)
        self.assertNotEqual(redone, before,
                            "redo must bring the signature back")
        self.session.undo()
        self.assertEqual(_render_digest(self.session), before)
        self.session.redo()
        self.assertEqual(_render_digest(self.session), redone,
                         "snapshot replay must be deterministic")
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "the raw file on disk must never change")

    def test_bad_inputs_are_refused_without_polluting_history(self) -> None:
        before = _render_digest(self.session)
        with self.assertRaises(ValueError):
            S.stamp_signature(self.session, 0, (200.0, 300.0, 200.0, 300.0),
                              self.png)
        with self.assertRaises(ValueError):
            S.stamp_signature(self.session, 0, (100.0, 100.0, 200.0, 150.0),
                              b"")
        with self.assertRaises(Exception):
            S.stamp_signature(self.session, 0, (100.0, 100.0, 200.0, 150.0),
                              b"not a png at all")
        self.assertEqual(_render_digest(self.session), before,
                         "a failed stamp must leave the document unchanged")
        self.assertFalse(self.session.can_undo,
                         "a failed stamp must not create a history entry")
        self.assertFalse(self.session.dirty)


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required to build fixtures")
@unittest.skipUnless(S.have_pyhanko(), "pyHanko is required for crypto tests")
class CryptoSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "saved.pdf"
        doc = D.fitz.open()
        doc.new_page(width=595, height=842).insert_text(
            (72, 100), "Document to be digitally signed")
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)

    def test_generate_sign_verify_then_detect_tampering(self) -> None:
        pfx = S.generate_self_signed(self.root / "certs", "FigView Test",
                                     b"pass-123")
        self.assertTrue(pfx.is_file())
        self.assertEqual(pfx.suffix, ".pfx")
        signed = self.root / "signed.pdf"
        result = S.crypto_sign(self.pdf, signed, pfx, b"pass-123",
                               reason="unit test")
        self.assertEqual(result, signed)
        self.assertTrue(signed.is_file())
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "signing must never modify the input file")
        reports = S.verify_signatures(signed)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertTrue(report.intact)
        self.assertTrue(report.ok)
        self.assertFalse(report.trusted,
                         "a self-signed certificate must report as untrusted")
        self.assertEqual(report.field_name, "FigViewSignature")
        self.assertIn("FigView Test", report.signer)
        self.assertTrue(report.status and report.status[0].isupper(),
                        "the report must carry a plain-language sentence")
        # Corrupt the file tail: bytes appended after signing mean the
        # signature no longer covers the entire file.
        with signed.open("ab") as handle:
            handle.write(b"% tampered tail bytes")
        tampered = S.verify_signatures(signed)
        self.assertTrue(tampered, "a signed file must still yield a report")
        self.assertTrue(any(not report.ok for report in tampered),
                        "verification must report the post-signing change")

    def test_verify_reports_nothing_for_an_unsigned_document(self) -> None:
        self.assertEqual(S.verify_signatures(self.pdf), [])

    def test_wrong_passphrase_is_an_actionable_error(self) -> None:
        pfx = S.generate_self_signed(self.root / "certs", "FigView Test",
                                     b"pass-123")
        with self.assertRaises(S.SigningError):
            S.crypto_sign(self.pdf, self.root / "never.pdf", pfx, b"wrong")
        self.assertFalse((self.root / "never.pdf").exists())

    def test_signing_in_place_is_refused(self) -> None:
        pfx = S.generate_self_signed(self.root / "certs", "FigView Test",
                                     b"pass-123")
        with self.assertRaises(S.SigningError):
            S.crypto_sign(self.pdf, self.pdf, pfx, b"pass-123")
        self.assertEqual(_sha(self.pdf), self.source_hash)

    def test_generated_certificates_never_clobber_each_other(self) -> None:
        first = S.generate_self_signed(self.root / "certs", "Same Name",
                                       b"pw-1")
        second = S.generate_self_signed(self.root / "certs", "Same Name",
                                        b"pw-2")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_generate_refuses_empty_identity_or_passphrase(self) -> None:
        with self.assertRaises(ValueError):
            S.generate_self_signed(self.root / "certs", "   ", b"pw")
        with self.assertRaises(ValueError):
            S.generate_self_signed(self.root / "certs", "Name", b"")


if __name__ == "__main__":
    unittest.main()
