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

BACKENDS = ("osmo", "sony", "fake")


def build_backend(name: str, **kwargs) -> CameraBackend:
    """Construct a backend by name.

    osmo and sony are imported lazily so the fake backend keeps working on
    hosts where neither real path can: WSL2 has no uvcvideo for the Osmo, and
    Windows has no python-gphoto2 for the Sony. The two cameras therefore run
    as two processes on two hosts, and `fake` is the only backend that runs
    everywhere.
    """
    if name == "fake":
        return FakeBackend(
            width=kwargs.get("width", 1280),
            height=kwargs.get("height", 720),
            fps=kwargs.get("fps", 30.0),
            settings=kwargs.get("settings", False),
        )
    if name == "sony":
        from .sony import SonyBackend

        backend = SonyBackend()
        if "keepalive" in kwargs:
            backend.keepalive_interval = float(kwargs["keepalive"])
        return backend
    if name == "osmo":
        from .osmo import OsmoBackend

        return OsmoBackend(
            device=kwargs.get("device", 0),
            width=kwargs.get("width", 1920),
            height=kwargs.get("height", 1080),
        )
    raise ValueError(f"unknown backend {name!r}; expected one of {BACKENDS}")
