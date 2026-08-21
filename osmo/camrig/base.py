"""Backend interface, and the grab-thread machinery every live-view backend shares.

The concurrency design here is load-bearing and is the thing most likely to be
broken by a well-meaning refactor. Read this before changing it:

Two callers reading the same cv2.VideoCapture -- which is exactly what a live
preview plus a running timelapse does -- produces torn frames or a hang. So
exactly one thread owns the device and writes each frame into a one-slot
buffer, and every consumer takes a copy out of that slot under a lock. Older
frames are dropped rather than queued, because a queue would let a slow
consumer add latency to a fast one.

The shutdown path is equally deliberate. Calling release() on a VideoCapture
while a thread sits blocked inside read() frees memory that is still in use --
a hard segfault, not an exception. So close() joins the grab thread with a
timeout and, if that join fails, returns *without* releasing. Leaking the
handle is the correct trade against killing the process.

On the UVC/DirectShow path there is no way to bound a blocked read at all:
CAP_PROP_READ_TIMEOUT_MSEC is silently rejected (measured against a Pocket 3,
2026-08-21 -- set() returns False). That property works on the FFMPEG-backed
network path, which is where it was originally learned. The substitute here is
a staleness watchdog: the grab thread stamps a monotonic clock after every
successful read, and `healthy` goes false when that stamp goes cold. It cannot
unblock the read, but it does let the API tell the truth about the device.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Raised when a device cannot be opened or has failed unrecoverably."""


@dataclass(frozen=True)
class Capabilities:
    """What a backend can actually do.

    The server and the UI read this and adapt, so a backend must never claim a
    capability it cannot honour -- a capability that lies is worse than one
    that is absent. Adding a feature means adding a field here, not a special
    case upstream.
    """

    live_view: bool = False
    still_capture: bool = False
    native_stills: bool = False
    settings: bool = False
    video_record: bool = False
    max_still: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["max_still"] = list(self.max_still) if self.max_still else None
        return d


@dataclass
class CaptureResult:
    """The outcome of one still.

    A failed capture returns ok=False; it does not raise. The timelapse loop
    counts failures and keeps going, because one bad frame must never end a
    400-frame run.
    """

    ok: bool
    path: str | None = None
    width: int | None = None
    height: int | None = None
    bytes: int | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CameraBackend(ABC):
    """Five methods. Everything above this reads `capabilities` and adapts."""

    name = "base"

    def __init__(self) -> None:
        self._state_lock = threading.RLock()
        self._opened = False
        self._last_error: str | None = None

    # -- lifecycle ------------------------------------------------------

    @abstractmethod
    def _acquire(self) -> None:
        """Open the device. Raise CameraError on failure."""

    @abstractmethod
    def _release(self) -> None:
        """Close the device. Must be safe to call twice."""

    def open(self) -> None:
        with self._state_lock:
            if self._opened:
                return
            try:
                self._acquire()
            except Exception as exc:
                self._last_error = str(exc)
                raise
            self._opened = True
            self._last_error = None

    def close(self) -> None:
        with self._state_lock:
            if not self._opened:
                return
            try:
                self._release()
            finally:
                self._opened = False

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- contract -------------------------------------------------------

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def read_frame(self) -> np.ndarray | None:
        """A BGR copy of the newest frame, or None if there is no live view."""

    @abstractmethod
    def capture_still(self, path: Path) -> CaptureResult: ...

    # -- optional -------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        return {}

    def set_setting(self, key: str, value: Any) -> None:
        raise CameraError(f"{self.name} backend has no settable {key!r}")

    @property
    def healthy(self) -> bool:
        return self._opened

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "opened": self.opened,
            "healthy": self.healthy,
            "last_error": self.last_error,
            "capabilities": self.capabilities.as_dict(),
        }

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class GrabThreadBackend(CameraBackend):
    """A backend whose frames come from a single thread polling a source.

    Subclasses implement _open_source / _grab / _close_source. Everything that
    makes concurrent access safe lives here so the osmo and fake backends share
    exactly one implementation of it -- which is what makes `fake` a real
    regression harness for the threading rather than a toy.
    """

    #: How long the newest frame may go stale before `healthy` turns false.
    stale_after = 3.0
    #: How long close() waits for the grab thread before leaking the handle.
    join_timeout = 2.0
    #: Pause after a failed grab, so a dead source does not spin a core.
    idle_sleep = 0.02

    jpeg_quality = 92

    def __init__(self) -> None:
        super().__init__()
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._frame_seq = 0
        self._last_frame_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: Any = None
        self._grab_failures = 0
        self._frames_grabbed = 0
        self._leaked = False
        # Bumped on every open. A grab thread that outlived its session keeps
        # its old epoch and is locked out of the buffer -- see _run.
        self._epoch = 0

    # -- subclass hooks -------------------------------------------------

    @abstractmethod
    def _open_source(self) -> Any:
        """Open and return the source handle. Raise CameraError on failure."""

    @abstractmethod
    def _grab(self, source: Any) -> np.ndarray | None:
        """Block for the next frame. None means this attempt failed.

        The handle is passed in rather than read off the instance so that a
        thread which outlived its session cannot end up reading whatever
        device the *next* session opened.
        """

    @abstractmethod
    def _close_source(self, source: Any) -> None: ...

    # -- lifecycle ------------------------------------------------------

    def _acquire(self) -> None:
        # A fresh Event per session, never a cleared one. Clearing the shared
        # Event would un-stop any thread still blocked from the last session
        # and bring it back to life against the new device.
        stop = threading.Event()
        self._leaked = False

        source = self._open_source()
        epoch = self._epoch + 1
        self._epoch = epoch
        self._stop = stop
        self._source = source

        seq_at_start = self._frame_seq
        thread = threading.Thread(
            target=self._run,
            args=(source, stop, epoch),
            name=f"{self.name}-grab-{epoch}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        # Wait for first light so open() fails loudly rather than handing back
        # a backend that will only look broken later. Compared against the
        # count at entry, not against zero -- on a reopen the lifetime counter
        # is already non-zero and would wave through a dead device.
        deadline = time.monotonic() + self.stale_after
        while time.monotonic() < deadline:
            if self._frame_seq > seq_at_start:
                return
            if not thread.is_alive():
                break
            time.sleep(0.02)

        stop.set()
        raise CameraError(
            f"{self.name}: opened the source but no frame arrived within "
            f"{self.stale_after:.0f}s"
        )

    def _release(self) -> None:
        stop = self._stop
        thread = self._thread
        source = self._source
        self._thread = None
        self._source = None
        stop.set()

        if thread is not None and thread.is_alive():
            thread.join(timeout=self.join_timeout)
            if thread.is_alive():
                # Deliberate. The thread is blocked inside a read we cannot
                # interrupt; releasing the source now would free memory it is
                # actively using and take the process down with it. Leaking is
                # the lesser cost, and it is bounded -- the handle dies when
                # the process does.
                #
                # The thread is not merely un-joined, it is disarmed: it holds
                # its own stop Event and its own source handle, and its epoch
                # is now stale, so it cannot touch the next session's device or
                # buffer no matter when its read finally returns.
                self._leaked = True
                log.warning(
                    "%s: grab thread did not exit within %.1fs; leaking the "
                    "device handle rather than risking a segfault in release()",
                    self.name,
                    self.join_timeout,
                )
                return
        if source is not None:
            self._close_source(source)

    def _run(self, source: Any, stop: threading.Event, epoch: int) -> None:
        while not stop.is_set():
            try:
                frame = self._grab(source)
            except Exception as exc:  # a source may die mid-read
                self._last_error = f"grab failed: {exc}"
                frame = None

            if frame is None:
                self._grab_failures += 1
                stop.wait(self.idle_sleep)
                continue

            with self._frame_lock:
                # A read that was in flight when the session ended lands here
                # afterwards. Publishing it would inject a frame from the old
                # device into the new session's buffer.
                if epoch != self._epoch:
                    break
                self._frame = frame
                self._frame_seq += 1
                self._last_frame_at = time.monotonic()
            self._frames_grabbed += 1

    # -- contract -------------------------------------------------------

    def read_frame(self) -> np.ndarray | None:
        """A private copy of the newest frame.

        Returns a copy, never the buffer itself -- handing out the slot would
        let a consumer read a frame the grab thread is overwriting.
        """
        with self._frame_lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def capture_still(self, path: Path) -> CaptureResult:
        frame = self.read_frame()
        if frame is None:
            return CaptureResult(ok=False, error="no frame available")

        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
            )
            if not ok:
                return CaptureResult(ok=False, error="JPEG encode failed")
            data = buf.tobytes()
            path.write_bytes(data)
        except Exception as exc:
            return CaptureResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        h, w = frame.shape[:2]
        return CaptureResult(
            ok=True,
            path=str(path),
            width=w,
            height=h,
            bytes=len(data),
            meta={"backend": self.name, "source": "video_frame"},
        )

    def encode_frame(self) -> bytes | None:
        """Newest frame as JPEG bytes, for the MJPEG stream."""
        frame = self.read_frame()
        if frame is None:
            return None
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        return buf.tobytes() if ok else None

    # -- health ---------------------------------------------------------

    @property
    def frame_age(self) -> float | None:
        if self._last_frame_at == 0.0:
            return None
        return time.monotonic() - self._last_frame_at

    @property
    def healthy(self) -> bool:
        if not self._opened:
            return False
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        age = self.frame_age
        return age is not None and age < self.stale_after

    def status(self) -> dict[str, Any]:
        st = super().status()
        age = self.frame_age
        st.update(
            {
                "frames_grabbed": self._frames_grabbed,
                "grab_failures": self._grab_failures,
                "frame_age": round(age, 3) if age is not None else None,
                "handle_leaked": self._leaked,
            }
        )
        return st
