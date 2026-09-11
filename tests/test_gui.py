"""Live Tk smoke test for switching between video and still-image modes."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from figureviewer import app as app_module
from figureviewer import docs as D
from figureviewer import video as V


@unittest.skipUnless(V.backend_available(), "OpenCV is not installed")
class ViewerWindowVideoTests(unittest.TestCase):
    def test_window_plays_video_then_releases_it_for_an_image(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_dir = root / "settings"
            video_path = root / "clip.avi"
            image_path = root / "figure.png"
            Image.new("RGB", (640, 360), "#2952a3").save(image_path)

            writer = cv2.VideoWriter(
                str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 24.0, (640, 360)
            )
            if not writer.isOpened():
                self.skipTest("The OpenCV build cannot create an MJPG test video")
            for index in range(60):
                frame = np.full((360, 640, 3), (20, 25, 35), dtype=np.uint8)
                cv2.rectangle(frame, (20 + index * 6, 130), (140 + index * 6, 230),
                              (240, 150, 30), -1)
                writer.write(frame)
            writer.release()

            settings_path = settings_dir / "settings.json"
            instance_path = settings_dir / "instance.json"
            defaults = {
                "single_instance": False,
                "thumb_size": 0,
                "geometry": "1100x720+40+40",
                "video_autoplay": True,
            }
            application = None
            with (mock.patch.object(app_module, "SETTINGS_DIR", settings_dir),
                  mock.patch.object(app_module, "SETTINGS_PATH", settings_path),
                  mock.patch.object(app_module, "INSTANCE_PATH", instance_path),
                  mock.patch.dict(app_module.DEFAULT_SETTINGS, defaults)):
                try:
                    application = app_module.App(root, video_path)
                except Exception as exc:
                    self.skipTest(f"Tk cannot open a live window in this environment: {exc}")
                window = application.windows[0]
                try:
                    deadline = time.perf_counter() + 4.0
                    while time.perf_counter() < deadline and window._video_photo is None:
                        application.root.update()
                        time.sleep(0.005)
                    self.assertIsNotNone(window._video_photo)
                    self.assertIsNotNone(window._video_session)
                    self.assertEqual(window.current.kind, D.KIND_VIDEO)
                    self.assertEqual([entry.path for entry in window.entries], [video_path])
                    self.assertEqual(window.folder_var.get(), "")
                    # Explicit files: the path box and its clear button give
                    # way to the folder chip.
                    self.assertFalse(window.clear_folder_button.winfo_ismapped())
                    self.assertTrue(window.path_chip.winfo_ismapped())
                    self.assertEqual(application.settings["recent_files"][0], str(video_path))
                    self.assertTrue(window.video_controls.winfo_ismapped())
                    self.assertIn(window._video_session.snapshot().state,
                                  {"playing", "paused", "ended"})

                    # Window dispatch matches the documented keyboard model:
                    # Left/Right are one frame and Up/Down use the setting.
                    session = window._video_session
                    session.stop_playback()
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and session.snapshot().frame_index != 0):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(session.snapshot().frame_index, 0)

                    window._next_action()
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and session.snapshot().frame_index != 1):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(session.snapshot().frame_index, 1)

                    window.video_frame_step.set(10)
                    window._video_jump_forward_or_next_file()
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and session.snapshot().frame_index != 11):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(session.snapshot().frame_index, 11)

                    window._video_jump_back_or_previous_file()
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and session.snapshot().frame_index != 1):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(session.snapshot().frame_index, 1)

                    window._previous_action()
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and session.snapshot().frame_index != 0):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(session.snapshot().frame_index, 0)

                    # Theme switching rebuilds every window. The old decoder
                    # must stop before the new light/dark window reopens the
                    # same selected video.
                    old_session = window._video_session
                    application.toggle_theme()
                    window = application.windows[0]
                    deadline = time.perf_counter() + 4.0
                    while time.perf_counter() < deadline and window._video_photo is None:
                        application.root.update()
                        time.sleep(0.005)
                    self.assertFalse(old_session._thread.is_alive())
                    self.assertIsNotNone(window._video_photo)

                    window.add_file(image_path)
                    deadline = time.perf_counter() + 2.0
                    while (time.perf_counter() < deadline
                           and image_path not in {entry.path for entry in window.entries}):
                        application.root.update()
                        time.sleep(0.005)
                    self.assertEqual(
                        [entry.path for entry in window.entries], [video_path, image_path]
                    )
                    self.assertEqual(application.settings["recent_files"][0], str(image_path))
                    image_entry = next(entry for entry in window.entries if entry.path == image_path)
                    window.load(image_entry)
                    deadline = time.perf_counter() + 3.0
                    while time.perf_counter() < deadline and window.document is None:
                        application.root.update()
                        time.sleep(0.005)
                    self.assertIsNotNone(window.document)
                    self.assertIsNone(window._video_session)
                    self.assertFalse(window.video_controls.winfo_ismapped())

                    # A folder is scanned only after an explicit folder action,
                    # and the small reset control genuinely exits that mode.
                    window.set_folder(root)
                    deadline = time.perf_counter() + 2.0
                    while time.perf_counter() < deadline and window.explicit_paths is not None:
                        application.root.update()
                        time.sleep(0.005)
                    deadline = time.perf_counter() + 2.0
                    while time.perf_counter() < deadline and len(window.entries) < 2:
                        application.root.update()
                        time.sleep(0.005)
                    self.assertIsNone(window.explicit_paths)
                    self.assertEqual(window.folder_var.get(), str(root))
                    application.root.update()
                    self.assertFalse(window.clear_folder_button.instate(["disabled"]))
                    self.assertTrue(window.folder_entry.winfo_ismapped())
                    self.assertFalse(window.path_chip.winfo_ismapped())
                    self.assertEqual({entry.path for entry in window.entries},
                                     {video_path, image_path})
                    self.assertEqual(application.settings["recent_folders"][0], str(root))

                    window.clear_folder_path()
                    application.root.update()
                    self.assertEqual(window.explicit_paths, [])
                    self.assertEqual(window.folder_var.get(), "")
                    self.assertEqual(window.entries, [])
                    application.root.update()
                    self.assertFalse(window.clear_folder_button.winfo_ismapped())
                    self.assertEqual(window.path_chip_var.get(), "Opened files")
                finally:
                    application.quit_all()


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class ViewerWindowPdfTests(unittest.TestCase):
    def test_pdf_reader_controls_wheel_zoom_and_sidebar_auto_hide(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_dir = root / "settings"
            pdf_path = root / "two-pages.pdf"
            pdf = pymupdf.open()
            pdf.new_page(width=595.28, height=841.89)
            pdf.new_page(width=841.89, height=595.28)
            pdf.save(pdf_path)
            pdf.close()

            application = None
            with (mock.patch.object(app_module, "SETTINGS_DIR", settings_dir),
                  mock.patch.object(app_module, "SETTINGS_PATH", settings_dir / "settings.json"),
                  mock.patch.object(app_module, "INSTANCE_PATH", settings_dir / "instance.json"),
                  mock.patch.dict(app_module.DEFAULT_SETTINGS, {
                      "single_instance": False,
                      "thumb_size": 0,
                      "geometry": "1100x720+40+40",
                      # This test pins the legacy A4 reading rule; the
                      # default is now the width-filling optimal zoom.
                      "open_fit": "reading",
                  })):
                try:
                    application = app_module.App(root, pdf_path)
                except Exception as exc:
                    self.skipTest(f"Tk cannot open a live window in this environment: {exc}")
                window = application.windows[0]
                try:
                    deadline = time.perf_counter() + 4.0
                    while time.perf_counter() < deadline and window.document is None:
                        application.root.update()
                        time.sleep(0.005)
                    application.root.update()
                    self.assertIsNotNone(window.document)
                    self.assertEqual(window.document.page_count, 2)
                    self.assertEqual(window.document.page_format(0), "A4 Portrait")
                    self.assertTrue(window.pdf_controls.winfo_ismapped())
                    self.assertIn("A4 Portrait", window.pdf_page_var.get())
                    displayed_zoom = window.scale * window.document.base_dpi / 96.0
                    self.assertAlmostEqual(displayed_zoom, 1.25, delta=0.03)

                    # Plain scrolling consumes the current page first, then
                    # advances only after scrolling continues at its edge.
                    window._on_wheel(SimpleNamespace(delta=-120, x=200, y=200))
                    self.assertEqual(window.page, 0)
                    self.assertGreater(window.off_y, 0)
                    _width, height = window.logical_size()
                    _cw, canvas_height = window._canvas_size()
                    window.off_y = height - canvas_height / window.scale
                    window._on_wheel(SimpleNamespace(delta=-120, x=200, y=200))
                    self.assertEqual(window.page, 1)
                    self.assertIn("A4 Landscape", window.pdf_page_var.get())

                    # Ctrl-scrolling zooms and leaves the page index intact.
                    before_scale = window.scale
                    window._on_zoom_wheel(SimpleNamespace(delta=120, x=200, y=200))
                    self.assertEqual(window.page, 1)
                    self.assertGreater(window.scale, before_scale)

                    # Scrolling upward at the top lands at the bottom of the
                    # previous page, preserving continuous reading direction.
                    window.off_y = 0
                    window._last_pdf_page_at = 0
                    window._on_wheel(SimpleNamespace(delta=120, x=200, y=200))
                    self.assertEqual(window.page, 0)
                    self.assertGreater(window.off_y, 0)

                    window.sidebar_auto_hide.set(True)
                    window._sidebar_auto_hide_changed()
                    self.assertTrue(window.show_sidebar.get())
                    window._auto_hide_sidebar()
                    self.assertFalse(window.show_sidebar.get())
                    window.sidebar_auto_hide.set(False)
                    window._sidebar_auto_hide_changed()
                    self.assertTrue(window.show_sidebar.get())
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
