"""Live Tk smoke test for switching between video and still-image modes."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
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
                    self.assertTrue(window.clear_folder_button.instate(["disabled"]))
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
                    self.assertFalse(window.clear_folder_button.instate(["disabled"]))
                    self.assertEqual({entry.path for entry in window.entries},
                                     {video_path, image_path})
                    self.assertEqual(application.settings["recent_folders"][0], str(root))

                    window.clear_folder_path()
                    application.root.update()
                    self.assertEqual(window.explicit_paths, [])
                    self.assertEqual(window.folder_var.get(), "")
                    self.assertEqual(window.entries, [])
                    self.assertTrue(window.clear_folder_button.instate(["disabled"]))
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
