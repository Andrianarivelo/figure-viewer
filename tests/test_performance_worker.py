"""Deterministic scheduling regressions without a Tk display or timing guesses."""
from __future__ import annotations

import threading
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from figureviewer.docs import Worker


class LazyRendererTests(unittest.TestCase):
    """Use fresh interpreters so other PDF tests cannot mask eager imports."""

    def run_fresh(self, source):
        """Execute an isolated backend scenario and preserve useful failures."""
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_raster_document_does_not_import_pdf_backend(self):
        self.run_fresh('''
            import io
            import sys
            from pathlib import Path
            from PIL import Image
            from figureviewer import docs as D
            data = io.BytesIO()
            Image.new("RGB", (16, 12)).save(data, format="PNG")
            entry = D.Entry(Path("test.png"), len(data.getvalue()), 0, D.KIND_IMAGE)
            document = D.RasterDocument(entry, data.getvalue())
            assert document.page_size(0) == (16, 12)
            document.close()
            assert not any(name == "fitz" or name.startswith("fitz.")
                           or name == "pymupdf" or name.startswith("pymupdf.")
                           for name in sys.modules), "PDF renderer imported for raster use"
        ''')

    def test_first_pdf_access_loads_proxy_once(self):
        self.run_fresh('''
            import types
            from unittest.mock import patch
            from figureviewer import docs as D
            backend = types.SimpleNamespace(open=lambda: "opened")
            D.HAVE_FITZ = True
            with patch.object(D.importlib, "import_module", return_value=backend) as load:
                assert D.fitz.open() == "opened"
                assert D.fitz.open() == "opened"
                load.assert_called_once_with("pymupdf")
        ''')

    def test_broken_backend_reports_error_at_pdf_open(self):
        self.run_fresh('''
            from pathlib import Path
            from unittest.mock import patch
            from figureviewer import docs as D
            D.HAVE_FITZ = True
            entry = D.Entry(Path("test.pdf"), 0, 0, D.KIND_DOC)
            with patch.object(D.importlib, "import_module",
                              side_effect=OSError("native renderer unavailable")):
                try:
                    D.VectorDocument(entry, b"")
                except RuntimeError as error:
                    assert "native renderer unavailable" in str(error)
                    assert "pip install" in str(error)
                else:
                    raise AssertionError("Broken backend did not raise a helpful error")
            assert not D.HAVE_FITZ
        ''')


class FakeRoot:
    """Record timer callbacks without running them outside the test thread."""

    def after(self, delay, callback):
        self.callback = callback
        return "timer"

    def after_cancel(self, identifier):
        self.callback = None


class WorkerSchedulingTests(unittest.TestCase):
    """Use events to establish worker order independently of machine speed."""

    def setUp(self):
        self.worker = Worker(FakeRoot(), max_workers=1)
        self.releases = []

    def tearDown(self):
        # Always release blocked workers, including when an assertion fails.
        for release in self.releases:
            release.set()
        self.worker.shutdown()

    def block_background(self):
        """Hold the sole background worker until the test explicitly releases it."""
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def block():
            started.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release its background worker")

        future = self.worker.submit(block, lambda result: None, background=True)
        self.assertTrue(started.wait(5))
        return release, future

    def test_foreground_bypasses_a_blocked_thumbnail_backlog(self):
        release, blocker = self.block_background()
        thumbnails = [self.worker.submit(lambda: "thumbnail", lambda value: None,
                                         background=True) for _ in range(300)]
        foreground = self.worker.submit(lambda: "opened", lambda value: None)
        self.assertEqual(foreground.result(timeout=5), "opened")
        self.assertFalse(blocker.done())
        self.assertTrue(all(not thumbnail.done() for thumbnail in thumbnails))
        release.set()

    def test_stale_queued_work_never_decodes(self):
        release, blocker = self.block_background()
        current = threading.Event()
        current.set()
        calls = []
        obsolete = self.worker.submit(lambda: calls.append("decoded"), calls.append,
                                      background=True, valid=current.is_set)
        current.clear()
        release.set()
        obsolete.result(timeout=5)
        self.worker._drain()
        self.assertEqual(calls, [])

    def test_stale_completed_result_is_not_delivered(self):
        current = threading.Event()
        current.set()
        delivered = []
        result = self.worker.submit(lambda: "old image", delivered.append,
                                    valid=current.is_set)
        result.result(timeout=5)
        current.clear()
        self.worker._drain()
        self.assertEqual(delivered, [])

    def test_foreground_callbacks_precede_background_callbacks(self):
        delivered = []
        background = self.worker.submit(lambda: "thumbnail", delivered.append,
                                        background=True)
        background.result(timeout=5)
        foreground = self.worker.submit(lambda: "opened", delivered.append)
        foreground.result(timeout=5)
        self.worker._drain()
        self.assertEqual(delivered, ["opened", "thumbnail"])

    def test_queued_future_can_be_cancelled(self):
        release, blocker = self.block_background()
        calls = []
        future = self.worker.submit(lambda: calls.append("decoded"), calls.append,
                                    background=True)
        self.assertTrue(future.cancel())
        release.set()
        blocker.result(timeout=5)
        self.worker._drain()
        self.assertEqual(calls, [])

    def test_failure_delivery_stays_on_the_main_thread(self):
        delivered = []
        main_thread = threading.get_ident()

        def fail():
            raise ValueError("decode failed")

        def record_failure(error):
            delivered.append((str(error), threading.get_ident()))

        result = self.worker.submit(fail, lambda value: None, record_failure)
        result.result(timeout=5)
        self.assertEqual(delivered, [])
        self.worker._drain()
        self.assertEqual(delivered, [("decode failed", main_thread)])

    def test_submission_after_shutdown_is_cancelled(self):
        self.worker.shutdown()
        future = self.worker.submit(lambda: "unused", lambda value: None)
        self.assertTrue(future.cancelled())


if __name__ == "__main__":
    unittest.main()
