"""OCR pipeline tests: packs, preprocessing, page OCR, invisible overlays.

Everything runs headless against fixtures generated on the fly with PyMuPDF;
no user file is ever read or written, and the one non-negotiable assertion is
repeated throughout: the bytes of a fixture on disk never change, and a
"make searchable" overlay never changes a single rendered pixel.

Engine-dependent tests are gated on :func:`figureviewer.ocr.ocr_available`
plus a real ``eng`` pack; each engine test copies that pack into a private
temp root and exercises the flat-to-fast migration on the copy, so the
app-owned tessdata directory is never touched either. Downloads are tested
against a mocked ``_urlopen`` seam and never reach the network.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from figureviewer import docs as D
from figureviewer import ocr as O
from figureviewer import pdfedit as E

#: The three fixture lines; 13 words, all unambiguous for Tesseract eng-fast.
FIXTURE_LINES = ("The quick brown fox",
                 "jumps over the lazy dog",
                 "Neural data analysis 2026")

_REAL_TESSDATA = O.default_tessdata_root()
_ENG_FILE = O.find_language_dir(_REAL_TESSDATA, "eng")
OCR_READY = bool(D.HAVE_FITZ and _ENG_FILE is not None
                 and O.ocr_available(_REAL_TESSDATA))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _write_native_pdf(path: Path) -> None:
    """A born-digital PDF: real text layer, no images."""
    doc = D.fitz.open()
    page = doc.new_page(width=360, height=216)
    for row, line in enumerate(FIXTURE_LINES):
        page.insert_text((36, 60 + 40 * row), line, fontsize=16)
    doc.save(str(path))
    doc.close()


def _write_scan_pdf(path: Path, dpi: int = 300) -> tuple[str, list]:
    """An image-only "scan" of the same content, plus its ground truth.

    Returns ``(reference_text, native_words)`` where the word boxes come from
    the text original; the scan page has the same point size and the render
    fills it edge to edge, so both live in the same coordinate frame.
    """
    fitz = D.fitz
    src = fitz.open()
    page = src.new_page(width=360, height=216)
    for row, line in enumerate(FIXTURE_LINES):
        page.insert_text((36, 60 + 40 * row), line, fontsize=16)
    reference = page.get_text("text")
    native_words = page.get_text("words")
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                          alpha=False)
    scan = fitz.open()
    target = scan.new_page(width=360, height=216)
    target.insert_image(target.rect, pixmap=pix)
    scan.save(str(path))
    scan.close()
    src.close()
    return reference, native_words


def _load(path: Path) -> D.Document:
    return D.load_document(_entry(path))


def _iou(a: tuple, b: tuple) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def _render_sha(document, page: int = 0, dpi: int = 150) -> str:
    """Byte-exact fingerprint of the rendered pixels at ``dpi``."""
    with document._lock:
        pix = document._page(page).get_pixmap(dpi=dpi, annots=True)
        digest = hashlib.sha256(bytes(pix.samples)).hexdigest()
        del pix
    return digest


class _FakeResponse:
    """Minimal stand-in for a urllib streaming response."""

    def __init__(self, chunks: list[bytes], content_length=None,
                 fail_after: int = -1) -> None:
        self._chunks = list(chunks)
        self._served = 0
        self._fail_after = fail_after
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int) -> bytes:
        if self._fail_after >= 0 and self._served >= self._fail_after:
            raise OSError("connection reset by peer")
        self._served += 1
        return self._chunks.pop(0) if self._chunks else b""


# ---------------------------------------------------------------------------
# Pure-python pieces: no engine, no network, no cv2 required.


class TransformTests(unittest.TestCase):
    def test_three_degree_rotation_round_trips_under_half_pixel(self) -> None:
        transform = O.Transform.rotate_scale(3.0, center=(749.5, 449.5),
                                             scale_x=2.0)
        for x, y in ((0.0, 0.0), (1499.0, 0.0), (0.0, 899.0),
                     (1499.0, 899.0), (321.5, 654.3), (749.5, 449.5)):
            px, py = transform.apply_point(x, y)
            bx, by = transform.invert_point(px, py)
            self.assertLess(abs(bx - x), 0.5)
            self.assertLess(abs(by - y), 0.5)

    def test_identity_scale_maps_and_inverts_exactly(self) -> None:
        transform = O.Transform.identity(2.0, 3.0)
        self.assertEqual(transform.apply_point(10.0, 10.0), (20.0, 30.0))
        self.assertEqual(transform.invert_point(20.0, 30.0), (10.0, 10.0))

    def test_singular_transform_refuses_to_invert(self) -> None:
        with self.assertRaises(ValueError):
            O.Transform((0.0, 0.0, 0.0, 0.0, 0.0, 0.0)).invert_point(1.0, 1.0)


class CerTests(unittest.TestCase):
    def test_identical_strings_have_zero_error(self) -> None:
        self.assertEqual(O.cer("neural data", "neural data"), 0.0)

    def test_known_levenshtein_distance(self) -> None:
        # kitten -> sitting: 3 edits over 6 reference characters.
        self.assertAlmostEqual(O.cer("kitten", "sitting"), 0.5)

    def test_empty_hypothesis_costs_the_whole_reference(self) -> None:
        self.assertEqual(O.cer("abcd", ""), 1.0)


# ---------------------------------------------------------------------------
# Preprocessing: needs cv2 (and PyMuPDF only to render realistic text).


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF renders the synthetic scans")
class PreprocessTests(unittest.TestCase):
    @classmethod
    def _text_render(cls, dpi: int = 300):
        from PIL import Image

        fitz = D.fitz
        doc = fitz.open()
        page = doc.new_page(width=360, height=216)
        for row, line in enumerate(FIXTURE_LINES):
            page.insert_text((36, 60 + 40 * row), line, fontsize=16)
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                              alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
        return image

    @unittest.skipUnless(O.preprocess_available(), "OpenCV is required")
    def test_deskew_corrects_a_three_degree_rotation(self) -> None:
        from PIL import Image

        skewed = self._text_render().convert("L").rotate(
            3.0, resample=Image.Resampling.BICUBIC, fillcolor=255)
        processed, transform = O.preprocess(skewed, src_dpi=300)
        # The recorded affine must carry a real rotation (sin(3 deg) ~ 0.052)
        # and still round-trip to sub-half-pixel accuracy.
        self.assertGreater(abs(transform.matrix[1]), 0.02)
        for point in ((100.0, 100.0), (1200.0, 700.0)):
            forward = transform.apply_point(*point)
            back = transform.invert_point(*forward)
            self.assertLess(abs(back[0] - point[0]), 0.5)
            self.assertLess(abs(back[1] - point[1]), 0.5)
        # And the output image must actually be straight again.
        cv2, np = O._opencv()
        arr = np.asarray(processed.convert("L"))
        _, binary = cv2.threshold(arr, 0, 255,
                                  cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        residual = O._estimate_skew(binary, cv2, np)
        self.assertLess(abs(residual), O.DESKEW_MIN_DEG)

    @unittest.skipUnless(O.preprocess_available(), "OpenCV is required")
    def test_straight_scan_at_target_dpi_keeps_identity_geometry(self) -> None:
        processed, transform = O.preprocess(self._text_render(), src_dpi=300)
        a, b, c, d, e, f = transform.matrix
        self.assertAlmostEqual(a, 1.0, places=3)
        self.assertAlmostEqual(e, 1.0, places=3)
        self.assertLess(abs(b) + abs(c) + abs(d) + abs(f), 1e-6)
        self.assertEqual(processed.size, self._text_render().size)

    def test_degrades_to_grayscale_upscale_without_cv2(self) -> None:
        image = self._text_render(dpi=150)
        with mock.patch.object(O, "_opencv", return_value=None):
            processed, transform = O.preprocess(image, src_dpi=150)
        self.assertEqual(processed.mode, "L")
        self.assertEqual(processed.size, (image.width * 2, image.height * 2))
        self.assertEqual(transform.apply_point(10.0, 20.0), (20.0, 40.0))
        self.assertEqual(transform.invert_point(20.0, 40.0), (10.0, 20.0))


# ---------------------------------------------------------------------------
# Pack management: pure file layout plus a mocked network.


class PackManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "tessdata"
        self.root.mkdir()

    def test_flat_layout_is_migrated_into_fast(self) -> None:
        (self.root / "xyz.traineddata").write_bytes(b"stub-pack-bytes")
        manager = O.PackManager(self.root)
        adopted = self.root / "fast" / "xyz.traineddata"
        self.assertTrue(adopted.is_file())
        self.assertEqual(adopted.read_bytes(), b"stub-pack-bytes")
        # Copy, not move: the flat original keeps working for older callers.
        self.assertTrue((self.root / "xyz.traineddata").is_file())
        self.assertIn("xyz", manager.installed("fast"))
        self.assertIn("fast/xyz", manager.packs())
        # Idempotent: a second construction adopts nothing new.
        self.assertEqual(O.PackManager(self.root).migrate(), [])

    def test_find_language_dir_accepts_flat_and_fast_layouts(self) -> None:
        self.assertIsNone(O.find_language_dir(self.root, "eng"))
        (self.root / "eng.traineddata").write_bytes(b"x")
        self.assertEqual(O.find_language_dir(self.root, "eng"), self.root)
        fast = self.root / "fast"
        fast.mkdir()
        (fast / "eng.traineddata").write_bytes(b"x")
        self.assertEqual(O.find_language_dir(self.root, "eng"), fast)

    def test_known_langs_is_a_curated_menu(self) -> None:
        self.assertIn("eng", O.KNOWN_LANGS)
        self.assertGreaterEqual(len(O.KNOWN_LANGS), 15)
        name, size_mb = O.KNOWN_LANGS["eng"]
        self.assertEqual(name, "English")
        self.assertGreater(size_mb, 0)

    def test_download_streams_to_part_then_replaces(self) -> None:
        manager = O.PackManager(self.root)
        chunks = [b"a" * 10, b"b" * 5]
        seen_urls: list[str] = []
        progress: list[tuple] = []

        def fake_urlopen(url: str) -> _FakeResponse:
            seen_urls.append(url)
            return _FakeResponse(chunks, content_length=15)

        with mock.patch.object(O, "_urlopen", fake_urlopen):
            final = manager.download("eng", "fast",
                                     progress_cb=lambda done, total:
                                     progress.append((done, total)))
        expected_url = O.TESSDATA_URL_TEMPLATES["fast"].format(code="eng")
        self.assertEqual(seen_urls, [expected_url])
        self.assertEqual(final, self.root / "fast" / "eng.traineddata")
        self.assertEqual(final.read_bytes(), b"a" * 10 + b"b" * 5)
        self.assertFalse((self.root / "fast" / "eng.traineddata.part").exists())
        self.assertEqual(progress, [(10, 15), (15, 15)])
        record = json.loads((self.root / "packs.json").read_text("utf-8"))
        self.assertEqual(record["fast/eng"]["bytes"], 15)
        self.assertEqual(record["fast/eng"]["source_url"], expected_url)

    def test_download_failure_raises_with_manual_escape_hatch(self) -> None:
        manager = O.PackManager(self.root)

        def fake_urlopen(url: str) -> _FakeResponse:
            return _FakeResponse([b"partial"], fail_after=1)

        with mock.patch.object(O, "_urlopen", fake_urlopen):
            with self.assertRaises(O.OcrPackError) as caught:
                manager.download("fra", "fast")
        message = str(caught.exception)
        self.assertIn(O.TESSDATA_URL_TEMPLATES["fast"].format(code="fra"),
                      message)
        self.assertIn(str(self.root / "fast"), message)
        self.assertFalse((self.root / "fast" / "fra.traineddata").exists())
        self.assertFalse((self.root / "fast" / "fra.traineddata.part").exists())
        self.assertNotIn("fast/fra", manager.packs())

    def test_download_cancel_discards_the_partial_file(self) -> None:
        manager = O.PackManager(self.root)
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(O, "_urlopen",
                               lambda url: _FakeResponse([b"data"])):
            with self.assertRaises(O.OcrPackError) as caught:
                manager.download("deu", "fast", cancel=cancel)
        self.assertIn("cancelled", str(caught.exception))
        self.assertEqual(manager.installed("fast"), [])

    def test_empty_download_is_treated_as_a_failure(self) -> None:
        manager = O.PackManager(self.root)
        with mock.patch.object(O, "_urlopen",
                               lambda url: _FakeResponse([])):
            with self.assertRaises(O.OcrPackError):
                manager.download("spa", "best")
        self.assertEqual(manager.installed("best"), [])

    def test_ocr_available_is_false_for_a_dir_without_eng(self) -> None:
        self.assertFalse(O.ocr_available(self.root))


# ---------------------------------------------------------------------------
# Engine tests: integrated Tesseract plus the migrated eng pack.


@unittest.skipUnless(OCR_READY,
                     "PyMuPDF with integrated Tesseract and the eng pack "
                     "are required for OCR engine tests")
class OcrEngineTests(unittest.TestCase):
    tessdata: Path
    fixtures: Path
    reference: str
    native_words: list

    @classmethod
    def setUpClass(cls) -> None:
        cls._class_tmp = tempfile.TemporaryDirectory()
        base = Path(cls._class_tmp.name)
        # Private tessdata root: copy the real eng pack FLAT, then prove the
        # PackManager migration adopts it into fast/ where OCR reads it.
        cls.tessdata = base / "tessdata"
        cls.tessdata.mkdir()
        assert _ENG_FILE is not None
        shutil.copyfile(_ENG_FILE / "eng.traineddata",
                        cls.tessdata / "eng.traineddata")
        manager = O.PackManager(cls.tessdata)
        assert manager.has("eng", "fast"), "migration must adopt eng"
        cls.fixtures = base / "fixtures"
        cls.fixtures.mkdir()
        cls.reference, cls.native_words = _write_scan_pdf(
            cls.fixtures / "scan.pdf")
        _write_native_pdf(cls.fixtures / "native.pdf")
        cls.scan_hash = _sha(cls.fixtures / "scan.pdf")
        cls.native_hash = _sha(cls.fixtures / "native.pdf")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_tmp.cleanup()

    def setUp(self) -> None:
        self.scan_pdf = self.fixtures / "scan.pdf"
        self.native_pdf = self.fixtures / "native.pdf"

    def tearDown(self) -> None:
        self.assertEqual(_sha(self.scan_pdf), self.scan_hash,
                         "the scan fixture on disk must never change")
        self.assertEqual(_sha(self.native_pdf), self.native_hash,
                         "the native fixture on disk must never change")

    def _norm(self, text: str) -> str:
        return " ".join(text.split())

    # -- page_ocr ----------------------------------------------------------
    def test_page_ocr_preprocessed_recovers_text_and_word_boxes(self) -> None:
        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        words = O.page_ocr(document, 0, lang="eng", variant="fast",
                           preprocess_on=True, dpi=300,
                           tessdata_dir=self.tessdata)
        hypothesis = self._norm(O.words_to_text(words))
        self.assertLess(O.cer(self._norm(self.reference), hypothesis), 0.05)
        # Every ground-truth word box must be matched by an OCR word of the
        # same text whose mapped-back box overlaps it with IoU > 0.5.
        for x0, y0, x1, y1, text, *_ in self.native_words:
            best = max((_iou((x0, y0, x1, y1), w.rect_pts)
                        for w in words if w.text == text), default=0.0)
            self.assertGreater(best, 0.5,
                               f"word {text!r} box was not recovered")

    def test_page_ocr_baseline_route_recovers_text(self) -> None:
        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        words = O.page_ocr(document, 0, lang="eng", variant="fast",
                           preprocess_on=False, dpi=300,
                           tessdata_dir=self.tessdata)
        hypothesis = self._norm(O.words_to_text(words))
        self.assertLess(O.cer(self._norm(self.reference), hypothesis), 0.05)
        for x0, y0, x1, y1, text, *_ in self.native_words:
            best = max((_iou((x0, y0, x1, y1), w.rect_pts)
                        for w in words if w.text == text), default=0.0)
            self.assertGreater(best, 0.5)

    def test_missing_language_pack_names_the_escape_hatch(self) -> None:
        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        with self.assertRaises(O.OcrPackError) as caught:
            O.page_ocr(document, 0, lang="zzz", variant="fast",
                       tessdata_dir=self.tessdata)
        message = str(caught.exception)
        self.assertIn("zzz", message)
        self.assertIn(str(self.tessdata), message)
        self.assertIn(O.TESSDATA_URL_TEMPLATES["fast"].format(code="zzz"),
                      message)

    # -- needs_ocr ---------------------------------------------------------
    def test_needs_ocr_distinguishes_scan_from_native(self) -> None:
        scan = _load(self.scan_pdf)
        self.addCleanup(scan.close)
        native = _load(self.native_pdf)
        self.addCleanup(native.close)
        self.assertTrue(O.needs_ocr(scan, 0))
        self.assertFalse(O.needs_ocr(native, 0))

    # -- make_searchable ---------------------------------------------------
    def test_make_searchable_adds_invisible_searchable_text(self) -> None:
        session = E.EditSession(_entry(self.scan_pdf))
        self.addCleanup(session.close)
        before_pixels = _render_sha(session.document, dpi=150)
        self.assertEqual(self._norm(session.document.text(0)), "")

        progress: list[tuple] = []
        report = O.make_searchable(
            session, lang="eng", variant="fast", preprocess_on=False,
            tessdata_dir=self.tessdata,
            progress_cb=lambda done, total: progress.append((done, total)))

        self.assertEqual(report.pages_done, 1)
        self.assertEqual(report.pages_skipped, 0)
        self.assertGreaterEqual(report.words_added, 10)
        self.assertFalse(report.cancelled)
        self.assertEqual(progress, [(1, 1)])

        # The overlay is invisible: the 150 dpi render is byte-identical.
        self.assertEqual(_render_sha(session.document, dpi=150), before_pixels)
        # ...while text extraction and search now work.
        hypothesis = self._norm(session.document.text(0))
        self.assertLess(O.cer(self._norm(self.reference), hypothesis), 0.05)
        with session.document._lock:
            hits = session.document._page(0).search_for("quick")
        self.assertGreaterEqual(len(hits), 1)

        # Exactly ONE undo entry, and a single undo removes the whole layer.
        self.assertTrue(session.dirty)
        self.assertEqual(len(session._history.undo), 1)
        self.assertEqual(session.undo_label, "Make searchable")
        session.undo()
        self.assertEqual(self._norm(session.document.text(0)), "")
        self.assertFalse(session.dirty)

    def test_make_searchable_skips_pages_with_native_text(self) -> None:
        session = E.EditSession(_entry(self.native_pdf))
        self.addCleanup(session.close)
        report = O.make_searchable(session, lang="eng", variant="fast",
                                   preprocess_on=False,
                                   tessdata_dir=self.tessdata)
        self.assertEqual(report.pages_done, 0)
        self.assertEqual(report.pages_skipped, 1)
        self.assertEqual(report.words_added, 0)
        self.assertFalse(session.can_undo)
        self.assertFalse(session.dirty)

    def test_cancel_leaves_no_history_entry_and_no_text(self) -> None:
        session = E.EditSession(_entry(self.scan_pdf))
        self.addCleanup(session.close)
        cancel = threading.Event()
        cancel.set()
        report = O.make_searchable(session, lang="eng", variant="fast",
                                   preprocess_on=False,
                                   tessdata_dir=self.tessdata, cancel=cancel)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.pages_done, 0)
        self.assertEqual(report.words_added, 0)
        self.assertFalse(session.can_undo)
        self.assertEqual(self._norm(session.document.text(0)), "")

    # -- cached text extraction -------------------------------------------
    def test_ocr_text_is_cached_per_content_version(self) -> None:
        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        cache: dict = {}
        calls: list[int] = []
        real_page_ocr = O.page_ocr

        def counting(doc, page, **kwargs):
            calls.append(page)
            return real_page_ocr(doc, page, **kwargs)

        with mock.patch.object(O, "page_ocr", counting):
            first = O.ocr_text(document, 0, cache, preprocess_on=False,
                               tessdata_dir=self.tessdata)
            second = O.ocr_text(document, 0, cache, preprocess_on=False,
                                tessdata_dir=self.tessdata)
            self.assertEqual(first, second)
            self.assertEqual(calls, [0], "the second read must be a cache hit")
            self.assertIn("quick", first)
            # A content change invalidates: same page, new version, and the
            # stale entry for that page is pruned.
            document.content_version += 1
            O.ocr_text(document, 0, cache, preprocess_on=False,
                       tessdata_dir=self.tessdata)
        self.assertEqual(calls, [0, 0])
        self.assertEqual(list(cache), [(0, document.content_version)])

    def test_make_text_provider_feeds_edit_session_fallback(self) -> None:
        session = E.EditSession(_entry(self.scan_pdf))
        self.addCleanup(session.close)
        session.ocr_text_provider = O.make_text_provider(
            tessdata_dir=self.tessdata, preprocess_on=False)
        text = self._norm(session.page_text(0))
        self.assertLess(O.cer(self._norm(self.reference), text), 0.05)

    # -- image OCR ---------------------------------------------------------
    def _fixture_image(self, dpi: int = 300):
        """The scan page rendered to a PIL image at ``dpi``, plus the
        ground-truth word boxes scaled into that image's pixel frame."""
        from PIL import Image

        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        fitz = D.fitz
        with document._lock:
            pix = document._page(0).get_pixmap(
                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height),
                                    bytes(pix.samples))
        factor = dpi / 72.0
        boxes = [(x0 * factor, y0 * factor, x1 * factor, y1 * factor, text)
                 for x0, y0, x1, y1, text, *_ in self.native_words]
        return image, boxes

    def test_image_ocr_recovers_text_and_pixel_boxes(self) -> None:
        image, boxes = self._fixture_image(300)
        words = O.image_ocr(image, lang="eng", variant="fast",
                            preprocess_on=True, tessdata_dir=self.tessdata,
                            dpi=(300, 300))
        hypothesis = self._norm(" ".join(w.text for w in words))
        self.assertLess(O.cer(self._norm(self.reference), hypothesis), 0.05)
        for x0, y0, x1, y1, text in boxes:
            best = max((_iou((x0, y0, x1, y1), w.rect_px)
                        for w in words if w.text == text), default=0.0)
            self.assertGreater(best, 0.5, f"word {text!r} box not recovered")
        # Reading order survives: the layout indices group the three lines.
        self.assertEqual(len({(w.block, w.line) for w in words}), 3)
        self.assertTrue(all(w.line >= 0 for w in words))

    def test_image_ocr_without_preprocessing_and_without_dpi(self) -> None:
        image, _boxes = self._fixture_image(300)
        image.info.pop("dpi", None)
        words = O.image_ocr(image, lang="eng", variant="fast",
                            preprocess_on=False, tessdata_dir=self.tessdata)
        hypothesis = self._norm(" ".join(w.text for w in words))
        self.assertLess(O.cer(self._norm(self.reference), hypothesis), 0.05)

    def test_page_ocr_words_carry_layout_indices(self) -> None:
        document = _load(self.scan_pdf)
        self.addCleanup(document.close)
        words = O.page_ocr(document, 0, lang="eng", variant="fast",
                           preprocess_on=True, dpi=300,
                           tessdata_dir=self.tessdata)
        self.assertEqual(len({(w.block, w.line) for w in words}), 3)

    def test_searchable_pdf_keeps_pixels_and_adds_text(self) -> None:
        from PIL import Image, ImageChops

        image, _boxes = self._fixture_image(300)
        words = O.image_ocr(image, lang="eng", variant="fast",
                            preprocess_on=True, tessdata_dir=self.tessdata,
                            dpi=(300, 300))
        data = O.searchable_pdf_from_image(image, words, dpi=300)
        fitz = D.fitz
        doc = fitz.open(stream=data, filetype="pdf")
        self.addCleanup(doc.close)
        self.assertEqual(doc.page_count, 1)
        page = doc[0]
        self.assertAlmostEqual(page.rect.width, image.width * 72 / 300, places=3)
        text = self._norm(page.get_text("text"))
        self.assertLess(O.cer(self._norm(self.reference), text), 0.05)
        self.assertTrue(page.search_for("quick"), "text must be searchable")
        # Invisible overlay: rendering the PDF at the image's resolution
        # gives the picture back, pixel for pixel.
        pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72.0, 300 / 72.0),
                              alpha=False)
        rendered = Image.frombytes("RGB", (pix.width, pix.height),
                                   bytes(pix.samples))
        self.assertEqual(rendered.size, image.size)
        diff = ImageChops.difference(rendered, image).getbbox()
        self.assertIsNone(diff, "the invisible text layer changed pixels")


class ImageDpiPolicyTests(unittest.TestCase):
    def test_declared_resolution_is_trusted_within_bounds(self) -> None:
        self.assertEqual(O.image_ocr_dpi((800, 600), (300, 300)), (300.0, 300.0))
        self.assertEqual(O.image_ocr_dpi((800, 600), (72, 72)), (72.0, 300.0))
        self.assertEqual(O.image_ocr_dpi((800, 600), (600, 600)), (600.0, 600.0))

    def test_missing_or_absurd_metadata_assumes_a_screen_capture(self) -> None:
        self.assertEqual(O.image_ocr_dpi((800, 600), None),
                         (O.ASSUMED_IMAGE_DPI, 300.0))
        self.assertEqual(O.image_ocr_dpi((800, 600), (1, 1)),
                         (O.ASSUMED_IMAGE_DPI, 300.0))
        self.assertEqual(O.image_ocr_dpi((800, 600), ("x",)),
                         (O.ASSUMED_IMAGE_DPI, 300.0))

    def test_upscale_is_capped_for_large_images(self) -> None:
        source, target = O.image_ocr_dpi((8000, 6000), None)
        self.assertEqual(source, O.ASSUMED_IMAGE_DPI)
        self.assertEqual(target, source)       # never below the source
        source, target = O.image_ocr_dpi((3000, 2000), None)
        self.assertAlmostEqual(target, source * O.OCR_MAX_SIDE_PX / 3000)
        self.assertLessEqual(3000 * target / source, O.OCR_MAX_SIDE_PX + 1e-6)


if __name__ == "__main__":
    unittest.main()
