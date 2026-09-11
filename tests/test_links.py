"""Clickable links: detection in text, extraction from PDFs, live behaviour.

Headless parts: address detection in words (URLs, e-mail, DOI, www, with
sentence punctuation and brackets stripped), the merge rule between a
PDF's own link annotations and detected text, and
:meth:`VectorDocument.links` on a generated PDF with a rotated source page,
a GoTo to a rotated target page, a named destination and a launch action.

The live part drives the real window: hand cursor and status on hover,
a click opens the address through a mocked ``webbrowser.open``, a drag
does not, an annotation link over blank page still opens after the pan
path, a page link jumps, and everything survives a rotated view.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import links as L
from figureviewer import linkui
from figureviewer.textlayer import TextLayer, Word


class AddressDetectionTests(unittest.TestCase):
    def test_urls_with_sentence_punctuation(self) -> None:
        cases = {
            "https://geforp.esup-portail.org": "https://geforp.esup-portail.org",
            "(https://example.org/a/b).": "https://example.org/a/b",
            "<http://example.org/x?q=1&r=2>,": "http://example.org/x?q=1&r=2",
            "https://en.wikipedia.org/wiki/Cat_(animal)": "https://en.wikipedia.org/wiki/Cat_(animal)",
            "ftp://ftp.ncbi.nlm.nih.gov/pub/": "ftp://ftp.ncbi.nlm.nih.gov/pub/",
            "HTTPS://Example.ORG": "HTTPS://Example.ORG",
        }
        for word, expected in cases.items():
            with self.subTest(word=word):
                self.assertEqual(L.address_in(word), expected)

    def test_mail_doi_and_www(self) -> None:
        self.assertEqual(L.address_in("prenom.nom@u-pariscite.fr."),
                         "mailto:prenom.nom@u-pariscite.fr")
        self.assertEqual(L.address_in("mailto:a@b.org"), "mailto:a@b.org")
        self.assertEqual(L.address_in("doi:10.1016/j.neuron.2020.01.001."),
                         "https://doi.org/10.1016/j.neuron.2020.01.001")
        self.assertEqual(L.address_in("10.1038/nature12373,"),
                         "https://doi.org/10.1038/nature12373")
        self.assertEqual(L.address_in("https://doi.org/10.1038/nature12373"),
                         "https://doi.org/10.1038/nature12373")
        self.assertEqual(L.address_in("www.example.org/path;"),
                         "https://www.example.org/path")

    def test_plain_words_are_not_links(self) -> None:
        for word in ("hello", "10.5", "3.14/2", "a@b", "http://", "www.",
                     "user@localhost", "e.g.", "Fig.2/3"):
            with self.subTest(word=word):
                self.assertIsNone(L.address_in(word))

    def test_openable_schemes_only(self) -> None:
        self.assertTrue(L.Link((0, 0, 1, 1), L.KIND_URI, "https://x.org").openable())
        self.assertTrue(L.Link((0, 0, 1, 1), L.KIND_URI, "mailto:a@b.org").openable())
        self.assertFalse(L.Link((0, 0, 1, 1), L.KIND_URI, "file:///C:/x.exe").openable())
        self.assertFalse(L.Link((0, 0, 1, 1), L.KIND_URI, "javascript:alert(1)").openable())
        self.assertFalse(L.Link((0, 0, 1, 1), L.KIND_OTHER, "run.exe").openable())
        self.assertTrue(L.Link((0, 0, 1, 1), L.KIND_PAGE, page=2).openable())
        self.assertFalse(L.Link((0, 0, 1, 1), L.KIND_PAGE, page=-1).openable())

    def test_merge_prefers_annotation_over_detected_text(self) -> None:
        layer = TextLayer([
            Word((10, 10, 100, 20), "https://a.org", 0, 0),
            Word((10, 40, 100, 50), "https://b.org", 0, 1),
            Word((10, 70, 60, 80), "plain", 0, 2),
        ])
        explicit = [L.Link((0, 5, 120, 25), L.KIND_URI, "https://annotated.org")]
        merged = L.LinkLayer.build(explicit, layer)
        self.assertEqual([link.target for link in merged],
                         ["https://annotated.org", "https://b.org"])
        self.assertIs(merged.hit(50, 15), merged.links[0])
        self.assertEqual(merged.hit(50, 45).origin, "text")
        self.assertIsNone(merged.hit(30, 75))
        self.assertEqual(len(L.LinkLayer.build([], None)), 0)


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _make_linked_pdf(path: Path, *, rotate_first: bool = False) -> None:
    """Page 1: a printed URL (no annotation), a URI link over blank page,
    a GoTo to page 2 (stored with /Rotate 90), a named destination and a
    launch action. Optionally page 1 itself is stored rotated."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=360, height=300)
    doc.new_page(width=360, height=300)
    doc[1].set_rotation(90)
    page = doc[0]
    page.insert_text((30, 50), "Visit https://example.org/docs/a). now", fontsize=14)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(30, 100, 120, 120),
                      "uri": "https://pymupdf.io"})
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(30, 140, 120, 160),
                      "page": 1, "to": pymupdf.Point(20, 250)})
    xref = doc[1].xref
    doc.xref_set_key(doc.pdf_catalog(), "Dests",
                     f"<< /target [ {xref} 0 R /XYZ 20 150 0 ] >>")
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_NAMED, "from": pymupdf.Rect(30, 180, 120, 200),
                      "name": "target"})
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_LAUNCH, "from": pymupdf.Rect(30, 220, 120, 240),
                      "file": "notes.txt"})
    if rotate_first:
        doc[0].set_rotation(90)
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class PdfLinkExtractionTests(unittest.TestCase):
    def test_kinds_targets_and_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "linked.pdf"
            _make_linked_pdf(path)
            document = D.VectorDocument(_entry(path), path.read_bytes(), base_dpi=144.0)
            try:
                found = document.links(0)
                factor = 2.0
                by_kind = {}
                for link in found:
                    by_kind.setdefault(link.kind, []).append(link)
                uri = by_kind[L.KIND_URI][0]
                self.assertEqual(uri.target, "https://pymupdf.io")
                self.assertEqual(uri.rect, (30 * factor, 100 * factor, 120 * factor, 120 * factor))
                goto, named = by_kind[L.KIND_PAGE]
                self.assertEqual(goto.page, 1)
                # (20, 250) on the unrotated target page renders at x = 300 - 250
                # = 50, y = 20 on the rotated page: the landing is 20 pt down.
                self.assertAlmostEqual(goto.y_pts, 20.0, places=3)
                self.assertEqual(named.page, 1)
                # The named /XYZ 20 150 is in PDF space (bottom-left origin):
                # 150 up from the bottom of a 300 pt page is 150 down, then
                # rotated by 90: the same 20 pt from the top.
                self.assertAlmostEqual(named.y_pts, 20.0, places=3)
                other = by_kind[L.KIND_OTHER][0]
                self.assertEqual(other.target, "notes.txt")
                self.assertFalse(other.openable())
                self.assertEqual(document.links(1), [])
                self.assertEqual(document.links(99), [])
            finally:
                document.close()

    def test_rotated_source_page_boxes_follow_the_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rotated.pdf"
            _make_linked_pdf(path, rotate_first=True)
            document = D.VectorDocument(_entry(path), path.read_bytes(), base_dpi=72.0)
            try:
                width, height = document.page_size_points(0)
                # 360 x 300 stored with /Rotate 90 renders portrait.
                self.assertEqual((width, height), (300.0, 360.0))
                uri = next(link for link in document.links(0) if link.kind == L.KIND_URI)
                # (30, 100, 120, 120) through a 90 degree rotation of a
                # 360 x 300 page: x' = 300 - y, y' = x.
                self.assertEqual(uri.rect, (180.0, 30.0, 200.0, 120.0))
                # And the printed URL's word box, taken from the text layer,
                # lands in the same frame (words() rotates them itself).
                words = TextLayer.from_words(document.words(0))
                detected = L.text_links(words)
                self.assertEqual(len(detected), 1)
                x0, y0, x1, y1 = detected[0].rect
                self.assertLess(x1, 300)
                self.assertGreater(y1 - y0, x1 - x0, "a rotated word is tall")
            finally:
                document.close()

    def test_raster_documents_have_no_annotation_links(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "figure.png"
            Image.new("RGB", (40, 30), "white").save(image_path)
            stat = image_path.stat()
            raster = D.RasterDocument(
                D.Entry(image_path, stat.st_size, stat.st_mtime, D.KIND_IMAGE),
                image_path.read_bytes())
            try:
                self.assertEqual(raster.links(0), [])
            finally:
                raster.close()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class LinkLiveTests(unittest.TestCase):
    def _start(self, stack: ExitStack, root: Path, target: Path):
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

    @staticmethod
    def _event(x: float, y: float, state: int = 0) -> SimpleNamespace:
        return SimpleNamespace(x=int(x), y=int(y), x_root=0, y_root=0, state=state)

    @staticmethod
    def _center(window, link) -> tuple[float, float]:
        x0, y0, x1, y1 = window.text._source_rect_to_canvas(link.rect)
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    def test_hover_click_drag_page_link_and_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "linked.pdf"
            _make_linked_pdf(pdf_path)
            opened: list[str] = []
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    linkui.webbrowser, "open",
                    lambda target, *a, **k: (opened.append(target), True)[1]))
                application = self._start(stack, root, pdf_path)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)
                    self._wait(application, lambda: window.links.count() >= 5)
                    controller = window.links
                    layer = controller.layer()
                    self.assertEqual(len(layer), 5)
                    printed = next(link for link in layer if link.origin == "text")
                    self.assertEqual(printed.target, "https://example.org/docs/a")
                    annotated = next(link for link in layer
                                     if link.kind == L.KIND_URI and link.origin == "annotation")
                    goto = next(link for link in layer if link.kind == L.KIND_PAGE)

                    # --- hover: hand cursor, status names the address.
                    px, py = self._center(window, printed)
                    window._on_motion(self._event(px, py))
                    self.assertEqual(str(window.canvas.cget("cursor")), linkui.CURSOR_LINK)
                    self.assertIn("https://example.org/docs/a", window.status_var.get())
                    self.assertTrue(window.canvas.find_withtag(linkui.TAG_HOVER))
                    window._on_motion(self._event(2, 2))
                    self.assertEqual(str(window.canvas.cget("cursor")), "")
                    self.assertFalse(window.canvas.find_withtag(linkui.TAG_HOVER))

                    # --- click on the printed URL opens it; no text stays selected.
                    window._on_press(self._event(px, py))
                    window._on_release(self._event(px + 1, py))
                    self.assertEqual(opened, ["https://example.org/docs/a"])
                    self.assertEqual(window.text.selected, [])

                    # --- a drag starting on the link selects text, opens nothing.
                    window._on_press(self._event(px, py))
                    window._on_drag(self._event(px + 60, py))
                    window._on_release(self._event(px + 60, py))
                    self.assertEqual(len(opened), 1)
                    self.assertTrue(window.text.selected)
                    window.text.clear_selection()

                    # --- annotation link over blank page: the press took the
                    # pan path, the release still opens it and ends the pan.
                    ax, ay = self._center(window, annotated)
                    window._on_press(self._event(ax, ay))
                    self.assertIsNotNone(window._pan_anchor)
                    window._on_release(self._event(ax, ay))
                    self.assertIsNone(window._pan_anchor)
                    self.assertEqual(opened[-1], "https://pymupdf.io")

                    # --- snip mode disables links entirely.
                    window.snip_mode.set(True)
                    self.assertIsNone(controller.link_at(px, py))
                    window.snip_mode.set(False)

                    # --- a rotated view keeps the link under the glyphs.
                    window.rotate(90)
                    application.root.update()
                    rx, ry = self._center(window, printed)
                    self.assertNotEqual((round(rx), round(ry)), (round(px), round(py)))
                    window._on_press(self._event(rx, ry))
                    window._on_release(self._event(rx, ry))
                    self.assertEqual(opened[-1], "https://example.org/docs/a")
                    window.rotate(-90)
                    application.root.update()

                    # --- page link jumps to page 2 near the target.
                    gx, gy = self._center(window, goto)
                    window._on_press(self._event(gx, gy))
                    window._on_release(self._event(gx, gy))
                    self.assertEqual(window.page, 1)
                    self.assertEqual(len(opened), 3)
                    self.assertEqual(controller.count(), 0)
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
