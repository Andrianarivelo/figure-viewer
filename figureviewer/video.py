"""Responsive local-video playback built on OpenCV's FFmpeg backend.

The Tk main loop must never decode, colour-convert, or resize a video frame.
``VideoSession`` therefore owns one decoder thread and exposes a deliberately
small thread-safe interface to the viewer:

* playback commands are written through a condition variable;
* decoded frames use a one-item mailbox, so stale frames are overwritten rather
  than queued and latency can never grow without bound;
* frames are resized to the current viewport before crossing to Tk;
* paused videos keep one source frame, allowing a resize to repaint immediately
  without touching the file again.

OpenCV is optional and imported only when a video is opened. Image/PDF startup
therefore stays just as light as before, and a missing backend becomes an
actionable in-app error rather than an import-time crash.

OpenCV provides efficient video decoding but no audio output. The session says
so explicitly through :attr:`VideoSnapshot.audio_available`; the UI never
pretends that a silent backend has working volume controls.
"""
from __future__ import annotations

import importlib.util
import math
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from PIL import Image

class VideoBackendError(RuntimeError):
    """Raised when OpenCV is missing or cannot open a selected video."""


@dataclass(frozen=True)
class VideoSnapshot:
    """Immutable state copied from the decoder thread for the Tk UI."""

    state: str = "loading"
    position: float = 0.0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    frame_count: int = 0
    dropped_frames: int = 0
    audio_available: bool = False
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.state not in ("loading", "error")

    @property
    def paused(self) -> bool:
        return self.state in ("paused", "ended")


def backend_available() -> bool:
    """Return whether an OpenCV installation is discoverable without loading it."""
    return importlib.util.find_spec("cv2") is not None


def format_clock(seconds: float) -> str:
    """Format seconds as ``M:SS`` or ``H:MM:SS`` for the transport bar."""
    value = max(0, int(seconds if math.isfinite(seconds) else 0))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def fitted_size(source: tuple[int, int], viewport: tuple[int, int]) -> tuple[int, int]:
    """Largest aspect-preserving size that fits ``source`` inside ``viewport``."""
    src_w, src_h = max(1, int(source[0])), max(1, int(source[1]))
    view_w, view_h = max(1, int(viewport[0])), max(1, int(viewport[1]))
    scale = min(view_w / src_w, view_h / src_h)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def _codec_name(value: float) -> str:
    """Turn OpenCV's packed FOURCC number into a readable short name."""
    try:
        number = int(value)
        text = "".join(chr((number >> (8 * index)) & 0xFF) for index in range(4))
        return "".join(ch for ch in text if 32 <= ord(ch) <= 126).strip()
    except Exception:
        return ""


def _open_capture(path: Path, cv2):
    """Open a local file with FFmpeg first, then let OpenCV try any backend."""
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    if capture.isOpened():
        return capture
    capture.release()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise VideoBackendError(
            "OpenCV could not decode this video. The codec may be unsupported "
            "by the installed FFmpeg build."
        )
    return capture


class VideoSession:
    """One local-file playback session with a dedicated decode thread.

    Tk calls the public methods from its UI thread. The decoder is the only
    thread that ever touches ``cv2.VideoCapture``. That ownership rule avoids
    backend races during seek, close, and rapid file navigation.
    """

    def __init__(self, path: Path, viewport: tuple[int, int], *,
                 autoplay: bool = True, loop: bool = False) -> None:
        self.path = Path(path)
        self._condition = threading.Condition()
        self._viewport = (max(1, int(viewport[0])), max(1, int(viewport[1])))
        self._paused = not autoplay
        self._loop = bool(loop)
        self._stop = False
        self._seek_to: Optional[float] = None
        self._rerender = False
        self._latest: Optional[tuple[int, Image.Image, float]] = None
        self._latest_sequence = 0
        self._last_source = None
        self._snapshot = VideoSnapshot()
        self._thread = threading.Thread(
            target=self._run, name=f"fv-video-{self.path.name}", daemon=True
        )
        self._thread.start()

    # -------------------------------------------------------------- commands
    def play(self) -> None:
        with self._condition:
            if self._snapshot.state == "ended":
                self._seek_to = 0.0
            self._paused = False
            if self._snapshot.state != "error":
                self._snapshot = replace(self._snapshot, state="playing")
            self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            self._paused = True
            if self._snapshot.state not in ("loading", "error", "ended"):
                self._snapshot = replace(self._snapshot, state="paused")
            self._condition.notify_all()

    def toggle(self) -> None:
        if self.snapshot().paused:
            self.play()
        else:
            self.pause()

    def stop_playback(self) -> None:
        """Pause and return to the first frame without destroying the session."""
        with self._condition:
            self._paused = True
            self._seek_to = 0.0
            self._condition.notify_all()

    def seek(self, seconds: float) -> None:
        with self._condition:
            duration = self._snapshot.duration
            upper = max(0.0, duration - 0.001) if duration else max(0.0, seconds)
            self._seek_to = max(0.0, min(float(seconds), upper))
            self._condition.notify_all()

    def seek_relative(self, seconds: float) -> None:
        current = self.snapshot().position
        self.seek(current + float(seconds))

    def set_loop(self, enabled: bool) -> None:
        with self._condition:
            self._loop = bool(enabled)
            self._condition.notify_all()

    def set_viewport(self, size: tuple[int, int]) -> None:
        viewport = (max(1, int(size[0])), max(1, int(size[1])))
        with self._condition:
            if viewport == self._viewport:
                return
            self._viewport = viewport
            self._rerender = True
            self._condition.notify_all()

    def close(self, timeout: float = 1.5) -> None:
        """Stop decoding and release the file handle."""
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, timeout))

    # --------------------------------------------------------------- readers
    def snapshot(self) -> VideoSnapshot:
        with self._condition:
            return replace(self._snapshot)

    def pop_frame(self) -> Optional[tuple[int, Image.Image, float]]:
        """Return and clear the newest frame, discarding any older presentation."""
        with self._condition:
            frame = self._latest
            self._latest = None
            return frame

    def current_frame(self) -> Optional[Image.Image]:
        """Copy the most recently decoded source frame for copy/save commands."""
        with self._condition:
            source = self._last_source
            if source is None:
                return None
            # Convert while holding the lock so the decoder cannot replace the
            # NumPy buffer midway through the copy.
            rgb = source[:, :, ::-1].copy()
        return Image.fromarray(rgb, mode="RGB")

    # -------------------------------------------------------------- internals
    def _set_error(self, message: str) -> None:
        with self._condition:
            self._paused = True
            self._snapshot = replace(self._snapshot, state="error", error=message)

    def _publish(self, source, position: float, cv2) -> None:
        """Resize and publish one frame, replacing any unpainted predecessor."""
        with self._condition:
            viewport = self._viewport
            self._last_source = source
            had_unpainted_frame = self._latest is not None

        height, width = source.shape[:2]
        out_w, out_h = fitted_size((width, height), viewport)
        if (out_w, out_h) != (width, height):
            interpolation = cv2.INTER_AREA if out_w < width or out_h < height else cv2.INTER_LINEAR
            displayed = cv2.resize(source, (out_w, out_h), interpolation=interpolation)
        else:
            displayed = source
        rgb = cv2.cvtColor(displayed, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb, mode="RGB")

        with self._condition:
            self._latest_sequence += 1
            dropped = self._snapshot.dropped_frames + int(had_unpainted_frame)
            self._latest = (self._latest_sequence, image, max(0.0, position))
            self._snapshot = replace(
                self._snapshot, position=max(0.0, position), dropped_frames=dropped
            )

    def _run(self) -> None:
        capture = None
        try:
            if not backend_available():
                raise VideoBackendError(
                    "Video playback requires OpenCV. Install it with:\n\n"
                    "    pip install opencv-python"
                )
            import cv2  # imported lazily to keep non-video startup fast

            capture = _open_capture(self.path, cv2)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            if not math.isfinite(fps) or fps < 1.0 or fps > 240.0:
                fps = 30.0
            frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
            width = max(0, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0))
            height = max(0, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
            duration = (frame_count / fps) if frame_count else 0.0
            codec = _codec_name(capture.get(cv2.CAP_PROP_FOURCC))

            with self._condition:
                state = "paused" if self._paused else "playing"
                self._snapshot = VideoSnapshot(
                    state=state, duration=duration, width=width, height=height,
                    fps=fps, codec=codec, frame_count=frame_count,
                    audio_available=False,
                )

            # Decode a poster immediately. The same frame becomes playback's
            # anchor, so autoplay starts without a blank flash.
            ok, source = capture.read()
            if not ok or source is None:
                raise VideoBackendError("The video contains no decodable frames.")
            position = max(0.0, float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0)
            self._publish(source, position, cv2)
            anchor_position = position
            anchor_wall = time.perf_counter()
            frame_period = 1.0 / fps

            while True:
                seek_target: Optional[float] = None
                rerender = False
                with self._condition:
                    if self._stop:
                        return
                    if self._seek_to is not None:
                        seek_target = self._seek_to
                        self._seek_to = None
                    rerender = self._rerender
                    self._rerender = False
                    paused = self._paused

                    if paused and seek_target is None and not rerender:
                        self._condition.wait(timeout=0.20)
                        continue

                if seek_target is not None:
                    capture.set(cv2.CAP_PROP_POS_MSEC, seek_target * 1000.0)
                    ok, source = capture.read()
                    if not ok or source is None:
                        # Some containers reject a seek to their exact final
                        # timestamp. Back off one frame before calling it an error.
                        fallback = max(0.0, seek_target - frame_period)
                        capture.set(cv2.CAP_PROP_POS_MSEC, fallback * 1000.0)
                        ok, source = capture.read()
                    if not ok or source is None:
                        raise VideoBackendError("Seeking failed for this video container.")
                    position = max(0.0, float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0)
                    self._publish(source, position, cv2)
                    anchor_position = position
                    anchor_wall = time.perf_counter()
                    with self._condition:
                        if self._paused:
                            self._snapshot = replace(self._snapshot, state="paused")
                    continue

                if rerender:
                    with self._condition:
                        source = self._last_source
                        position = self._snapshot.position
                    if source is not None:
                        self._publish(source, position, cv2)
                    if paused:
                        continue

                # Presentation is clocked from monotonic time. When decoding or
                # Tk painting falls behind, grab frames without converting them
                # until the stream is near the target. Latency stays low while
                # CPU and memory remain bounded.
                now = time.perf_counter()
                target_position = anchor_position + (now - anchor_wall)
                current_frame = max(0, int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0))
                target_frame = max(current_frame, int(target_position * fps))
                skipped = 0
                while target_frame - current_frame > 1 and skipped < 12:
                    if not capture.grab():
                        break
                    current_frame += 1
                    skipped += 1

                ok, source = capture.read()
                if not ok or source is None:
                    with self._condition:
                        should_loop = self._loop and not self._stop
                        if should_loop:
                            self._seek_to = 0.0
                        else:
                            self._paused = True
                            self._snapshot = replace(
                                self._snapshot, state="ended",
                                position=self._snapshot.duration or self._snapshot.position,
                            )
                    if should_loop:
                        continue
                    continue

                position = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
                if position <= 0.0:
                    position = float(capture.get(cv2.CAP_PROP_POS_FRAMES) or 1) / fps
                self._publish(source, position, cv2)

                with self._condition:
                    if self._snapshot.state != "error":
                        self._snapshot = replace(self._snapshot, state="playing")
                    # Wait only until this frame's target time. A command or
                    # seek wakes the condition immediately.
                    deadline = anchor_wall + max(0.0, position - anchor_position)
                    delay = min(frame_period, max(0.0, deadline - time.perf_counter()))
                    if delay > 0:
                        self._condition.wait(timeout=delay)
        except VideoBackendError as exc:
            self._set_error(str(exc))
        except Exception as exc:  # backend failures must not crash Tk
            self._set_error(f"Video playback failed: {exc}")
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass


def thumbnail(path: Path, size: int) -> Image.Image:
    """Decode one representative frame without starting a playback session."""
    if not backend_available():
        return placeholder_thumbnail(size)
    try:
        import cv2

        capture = _open_capture(Path(path), cv2)
        try:
            frames = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
            if frames > 12:
                capture.set(cv2.CAP_PROP_POS_FRAMES, min(frames - 1, max(1, frames // 10)))
            ok, frame = capture.read()
            if not ok or frame is None:
                return placeholder_thumbnail(size)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb, mode="RGB")
            image.thumbnail((size, size), Image.Resampling.BILINEAR, reducing_gap=2.0)
            return image.copy()
        finally:
            capture.release()
    except Exception:
        return placeholder_thumbnail(size)


def placeholder_thumbnail(size: int) -> Image.Image:
    """Small deterministic video tile used when a poster cannot be decoded."""
    from PIL import ImageDraw

    edge = max(8, int(size))
    image = Image.new("RGB", (edge, edge), "#14213d")
    draw = ImageDraw.Draw(image)
    pad = max(2, edge // 5)
    draw.rounded_rectangle(
        (1, 1, edge - 2, edge - 2), radius=max(2, edge // 5),
        outline="#27d3e2", width=max(1, edge // 12),
    )
    draw.polygon(
        ((pad + 1, pad), (edge - pad, edge // 2), (pad + 1, edge - pad)),
        fill="#ffffff",
    )
    return image
