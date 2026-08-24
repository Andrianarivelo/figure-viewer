"""OpenCV playback-session tests using a generated, non-user video."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from figureviewer import video as V


class VideoHelpersTests(unittest.TestCase):
    def test_clock_format(self) -> None:
        self.assertEqual(V.format_clock(0), "0:00")
        self.assertEqual(V.format_clock(61.9), "1:01")
        self.assertEqual(V.format_clock(3661), "1:01:01")

    def test_aspect_fit(self) -> None:
        self.assertEqual(V.fitted_size((1920, 1080), (800, 600)), (800, 450))
        self.assertEqual(V.fitted_size((720, 1280), (800, 600)), (338, 600))


@unittest.skipUnless(V.backend_available(), "OpenCV is not installed")
class VideoSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import cv2
        import numpy as np

        cls._temporary = tempfile.TemporaryDirectory()
        cls.path = Path(cls._temporary.name) / "generated-playback-test.avi"
        width, height, fps, count = 960, 540, 30.0, 120
        writer = cv2.VideoWriter(
            str(cls.path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height)
        )
        if not writer.isOpened():
            cls._temporary.cleanup()
            raise unittest.SkipTest("The OpenCV build cannot create an MJPG test video")
        for index in range(count):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[:] = (25 + index % 50, 25, 42)
            x = 60 + (index * 7) % 800
            cv2.circle(frame, (x, height // 2), 45, (240, 180, 30), -1, cv2.LINE_AA)
            writer.write(frame)
        writer.release()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _wait_for(self, session: V.VideoSession, predicate, timeout: float = 2.5):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            snapshot = session.snapshot()
            if snapshot.state == "error":
                self.fail(snapshot.error)
            if predicate(snapshot):
                return snapshot
            time.sleep(0.01)
        self.fail(f"Timed out with state {session.snapshot()}")

    def test_play_seek_pause_resize_and_close(self) -> None:
        session = V.VideoSession(self.path, (800, 500), autoplay=True)
        try:
            ready = self._wait_for(session, lambda state: state.ready and state.width > 0)
            self.assertEqual((ready.width, ready.height), (960, 540))
            self.assertAlmostEqual(ready.fps, 30.0, delta=1.0)
            advanced = self._wait_for(session, lambda state: state.position >= 0.6)
            self.assertEqual(advanced.state, "playing")

            session.pause()
            self._wait_for(session, lambda state: state.state == "paused")
            session.seek(2.0)
            seeked = self._wait_for(session, lambda state: 1.7 <= state.position <= 2.4)
            self.assertTrue(seeked.paused)

            # Frame commands are index-based, pause playback, and preserve
            # their exact delta in both directions.
            one_forward = min(seeked.frame_count - 1, seeked.frame_index + 1)
            session.step_frames(1)
            stepped = self._wait_for(
                session, lambda state: state.frame_index == one_forward
            )
            self.assertTrue(stepped.paused)

            ten_forward = min(stepped.frame_count - 1, stepped.frame_index + 10)
            session.step_frames(10)
            jumped = self._wait_for(
                session, lambda state: state.frame_index == ten_forward
            )
            self.assertTrue(jumped.paused)

            session.step_frames(-10)
            returned = self._wait_for(
                session, lambda state: state.frame_index == stepped.frame_index
            )
            self.assertTrue(returned.paused)

            # Clear the pre-resize mailbox, then verify that a paused video is
            # re-rendered to the new viewport without another decode.
            session.pop_frame()
            session.set_viewport((400, 300))
            deadline = time.perf_counter() + 1.0
            resized = None
            while time.perf_counter() < deadline and resized is None:
                resized = session.pop_frame()
                time.sleep(0.01)
            self.assertIsNotNone(resized)
            self.assertEqual(resized[1].size, (400, 225))
        finally:
            session.close()

    def test_thumbnail_is_bounded(self) -> None:
        image = V.thumbnail(self.path, 48)
        self.assertLessEqual(image.width, 48)
        self.assertLessEqual(image.height, 48)
        self.assertGreater(image.width * image.height, 0)


if __name__ == "__main__":
    unittest.main()
