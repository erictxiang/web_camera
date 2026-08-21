"""Interval capture on an absolute schedule.

The runner is just another consumer of the frame buffer -- it calls
capture_still() exactly like a manual snapshot does, which is what lets a
preview stream and a timelapse run at the same time.

Two rules:

  * Schedule against `start + n * interval`, never `sleep(interval)`. Sleeping
    the interval accumulates the capture duration into every subsequent frame,
    so a long run drifts by however long the captures took in total.
  * A failed capture is counted and the run continues. One bad frame must
    never end a 400-frame sequence.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import CameraBackend, CaptureResult

log = logging.getLogger(__name__)


class TimelapseError(RuntimeError):
    pass


@dataclass
class TimelapseState:
    running: bool = False
    name: str | None = None
    directory: str | None = None
    interval: float = 0.0
    target: int | None = None
    captured: int = 0
    failed: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    next_at: float | None = None
    last_error: str | None = None
    stopped_by_user: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        now = time.time()
        if self.running and self.next_at is not None:
            d["seconds_to_next"] = round(max(0.0, self.next_at - now), 2)
        else:
            d["seconds_to_next"] = None
        if self.started_at is not None:
            end = self.finished_at if self.finished_at else now
            d["elapsed"] = round(end - self.started_at, 2)
        else:
            d["elapsed"] = None
        return d


class TimelapseRunner:
    """Owns at most one run at a time."""

    def __init__(self, backend: CameraBackend, root: Path) -> None:
        self.backend = backend
        self.root = Path(root)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = TimelapseState()

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
            return thread is not None and thread.is_alive()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state.as_dict()

    def start(
        self,
        interval: float,
        count: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise TimelapseError("a timelapse is already running")

            if not self.backend.capabilities.still_capture:
                raise TimelapseError(
                    f"{self.backend.name} backend cannot capture stills"
                )

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            folder = f"{stamp}-{name}" if name else stamp
            directory = self.root / folder
            directory.mkdir(parents=True, exist_ok=True)

            self._stop.clear()
            self._state = TimelapseState(
                running=True,
                name=name,
                directory=str(directory),
                interval=interval,
                target=count,
                started_at=time.time(),
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(directory, interval, count),
                name="timelapse",
                daemon=True,
            )
            self._thread.start()
            return self._state.as_dict()

    def stop(self, timeout: float = 5.0) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            self._state.stopped_by_user = True
            return self._state.as_dict()

    def _run(self, directory: Path, interval: float, count: int | None) -> None:
        # Absolute schedule. Each frame is due at start + n * interval, so the
        # time a capture takes is absorbed by the following wait instead of
        # being added to it.
        start = time.monotonic()
        n = 0
        try:
            while not self._stop.is_set():
                if count is not None and n >= count:
                    break

                due = start + n * interval
                delay = due - time.monotonic()
                if delay > 0 and self._stop.wait(delay):
                    break

                path = directory / f"frame_{n:05d}.jpg"
                try:
                    result = self.backend.capture_still(path)
                except Exception as exc:
                    # A backend should return ok=False rather than raise, but a
                    # run must survive one that does not honour that.
                    result = CaptureResult(
                        ok=False, error=f"{type(exc).__name__}: {exc}"
                    )

                with self._lock:
                    if result.ok:
                        self._state.captured += 1
                    else:
                        self._state.failed += 1
                        self._state.last_error = result.error
                        log.warning(
                            "timelapse frame %d failed: %s -- continuing",
                            n,
                            result.error,
                        )
                    n += 1
                    self._state.next_at = (
                        time.time() + max(0.0, (start + n * interval) - time.monotonic())
                    )
        finally:
            with self._lock:
                self._state.running = False
                self._state.finished_at = time.time()
                self._state.next_at = None
