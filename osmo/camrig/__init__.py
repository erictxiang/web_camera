"""camrig -- one web UI and one REST API over cameras that share nothing."""

from .base import (
    Capabilities,
    CameraBackend,
    CameraError,
    CaptureResult,
    GrabThreadBackend,
)
from .fake import FakeBackend
from .timelapse import TimelapseError, TimelapseRunner

__all__ = [
    "Capabilities",
    "CameraBackend",
    "CameraError",
    "CaptureResult",
    "GrabThreadBackend",
    "FakeBackend",
    "TimelapseError",
    "TimelapseRunner",
    "build_backend",
]

BACKENDS = ("osmo", "fake")


def build_backend(name: str, **kwargs) -> CameraBackend:
    """Construct a backend by name.

    osmo is imported lazily so the fake backend keeps working on hosts where
    the UVC path cannot -- WSL2, most notably, whose kernel has no uvcvideo.
    """
    if name == "fake":
        return FakeBackend(
            width=kwargs.get("width", 1280),
            height=kwargs.get("height", 720),
            fps=kwargs.get("fps", 30.0),
        )
    if name == "osmo":
        from .osmo import OsmoBackend

        return OsmoBackend(
            device=kwargs.get("device", 0),
            width=kwargs.get("width", 1920),
            height=kwargs.get("height", 1080),
        )
    raise ValueError(f"unknown backend {name!r}; expected one of {BACKENDS}")
