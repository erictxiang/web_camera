"""Synthetic frames, so the whole stack runs and is tested with no hardware.

This is the regression harness, not a toy. It drives the same grab thread, the
same one-slot buffer and the same shutdown path as the real camera, which is
what makes it able to catch a broken refactor of the concurrency design.

The injectable failure modes exist because those paths are the ones no manual
test ever reaches: a source that stalls, a capture that fails mid-run, a device
that will not open at all.
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np

from .base import Capabilities, CameraError, CaptureResult, GrabThreadBackend


class FakeBackend(GrabThreadBackend):
    name = "fake"

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: float = 30.0,
        fail_open: bool = False,
        fail_every: int = 0,
        stall_after: int = 0,
    ) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self.fps = fps
        #: Raise on open, to exercise the error path through /api/status.
        self.fail_open = fail_open
        #: Fail every Nth capture_still, to prove a timelapse survives it.
        self.fail_every = fail_every
        #: Stop producing frames after N, to exercise the staleness watchdog.
        self.stall_after = stall_after
        self._n = 0
        self._captures = 0
        self._capture_lock = threading.Lock()

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            live_view=True,
            still_capture=True,
            native_stills=False,
            settings=False,
            video_record=False,
            max_still=(self.width, self.height),
        )

    def _open_source(self) -> None:
        if self.fail_open:
            raise CameraError("fake: fail_open was set")
        self._n = 0

    def _grab(self) -> np.ndarray | None:
        if self.stall_after and self._n >= self.stall_after:
            time.sleep(0.05)
            return None

        time.sleep(1.0 / self.fps)
        self._n += 1
        return self._draw(self._n)

    def _draw(self, n: int) -> np.ndarray:
        """A frame whose content changes every time, so torn or stale frames
        are detectable rather than plausible."""
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        # A moving gradient: every pixel differs between consecutive frames, so
        # a test can assert a frame is internally consistent.
        phase = (n * 4) % 256
        col = np.arange(self.width, dtype=np.uint16)
        frame[:, :, 0] = ((col + phase) % 256).astype(np.uint8)
        frame[:, :, 1] = np.uint8(phase)
        row = np.arange(self.height, dtype=np.uint16).reshape(-1, 1)
        frame[:, :, 2] = ((row + phase) % 256).astype(np.uint8)

        cv2.putText(
            frame,
            f"fake #{n}",
            (30, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (255, 255, 255),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            time.strftime("%H:%M:%S"),
            (30, self.height - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        # Tear-detection marker: a solid block of one value, drawn last so no
        # text or antialiasing touches it. Every pixel in it comes from the
        # same frame, so more than one distinct value in this region means a
        # reader saw two generations at once. The gradient above cannot serve
        # this purpose -- LINE_AA text blends hundreds of intermediate values
        # into every channel.
        frame[: self.MARKER, -self.MARKER :] = np.uint8(phase)
        return frame

    #: Side length of the tear-detection block, top-right corner.
    MARKER = 32

    def marker_values(self, frame: np.ndarray) -> np.ndarray:
        """Distinct values inside the tear marker. Size 1 means a clean frame."""
        return np.unique(frame[: self.MARKER, -self.MARKER :])

    def _close_source(self) -> None:
        pass

    def capture_still(self, path) -> CaptureResult:
        with self._capture_lock:
            self._captures += 1
            n = self._captures
        if self.fail_every and n % self.fail_every == 0:
            # Return, do not raise. The timelapse counts this and continues.
            return CaptureResult(ok=False, error=f"fake: injected failure on #{n}")
        return super().capture_still(path)

    @property
    def frame_number(self) -> int:
        return self._n
