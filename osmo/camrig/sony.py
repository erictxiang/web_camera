"""Sony a6000 over PTP, through libgphoto2.

Measured against the real body on 2026-09-04 (see ../../sony/README.md):

  * libgphoto2 2.5.33 does not show the 2.5.31 read-only regression. Every
    exposure widget is writable with the mode dial on M, and set/get round
    trips land: shutter 1/250, f/5.6, ISO 400 all read back as written.
  * With the dial on anything but M the body itself locks shutter, aperture,
    ISO and compensation. That looks identical to the regression from the
    API side, so `status()` reports the dial position rather than guessing.
  * Capture, download and delete take about 6 s and produce a 6000x4000 JPEG.
    No hang on the delete step.
  * The body sleeps on its own power-save timer and re-enumerates as a USB HID
    device (054c:0994 instead of 054c:094e). Every PTP call then fails until
    it is woken by hand. The keepalive thread here holds one session open and
    polls it, which is the mechanism Sony's own remote software relies on to
    keep the body awake; whether it is sufficient at every timer setting is
    still to be measured with a long soak. When the link does drop, the same
    thread retries the connection so a wake plus usbipd auto-attach recovers
    without a restart.

Choices worth knowing:

  * ISO tokens are the labels libgphoto2 prints, so auto is "Auto ISO" and not
    "Auto". `set_setting` matches loosely against the choice list so a caller
    can pass "5.6" for "f/5.6", but the value it returns is always the exact
    token the camera reported back.
  * There is no live view over USB on this body, so `read_frame` is None and
    `live_view` is False. The UI hides the viewport off that flag.
  * libgphoto2 is not thread-safe. Every call goes through one lock, and a
    capture holds it for the whole 6 s. Settings reads queue behind it, which
    is the right trade: a torn config read is worse than a late one.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .base import Capabilities, CameraBackend, CameraError, CaptureResult

log = logging.getLogger(__name__)

try:  # keep camrig importable on Windows, where the bindings are absent
    import gphoto2 as _gp
except ImportError:  # pragma: no cover - exercised on hosts without gphoto2
    _gp = None


#: The settings the API exposes, in the order the UI shows them. Anything
#: else in the config tree is deliberately unreachable: the tree has ~60
#: widgets and most of them are either read-only PTP mirrors or things that
#: should never be flipped from a web page (USB mode, format card, ...).
SETTINGS: tuple[str, ...] = (
    "expprogram",
    "shutterspeed",
    "f-number",
    "iso",
    "exposurecompensation",
    "whitebalance",
    "focusmode",
    "imagequality",
    "capturemode",
)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from the SOF marker, without decoding 24 MP."""
    i = 2
    n = len(data)
    while i + 9 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        i += 2 + length
    return None


def match_choice(value: Any, choices: list[str]) -> str | None:
    """The exact camera token for a loosely written value, or None.

    Exact match first, then case-insensitive, then a match after stripping
    the "f/" prefix so "5.6" finds "f/5.6". Never guesses beyond that: an
    ambiguous value is an error the caller should see, not a nearby setting
    silently applied.
    """
    text = str(value).strip()
    if text in choices:
        return text
    folded = text.lower()
    for c in choices:
        if c.lower() == folded:
            return c
    stripped = folded[2:] if folded.startswith("f/") else folded
    hits = [c for c in choices if (c.lower()[2:] if c.lower().startswith("f/") else c.lower()) == stripped]
    return hits[0] if len(hits) == 1 else None


class SonyBackend(CameraBackend):
    name = "sony"

    #: Seconds between keepalive polls of the open session.
    keepalive_interval = 5.0
    #: How long the last successful camera call may be before `healthy` turns false.
    stale_after = 30.0
    #: How long a set_setting waits for the camera to report the new value.
    settle = 2.0
    #: How long close() waits for the keepalive thread before leaking the session.
    join_timeout = 3.0

    def __init__(
        self,
        max_still: tuple[int, int] = (6000, 4000),
        require_control: bool = True,
        gp: Any = None,
    ) -> None:
        super().__init__()
        self._gp = gp if gp is not None else _gp
        self.max_still = max_still
        #: Refuse to open a body that is in MTP or mass-storage mode. Those
        #: modes can list files but cannot fire the shutter or write settings,
        #: and the fix is a menu on the camera, so say so at open() time.
        self.require_control = require_control

        self._lock = threading.RLock()
        self._camera: Any = None
        self._model: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ok = 0.0
        self._keepalive_failures = 0
        self._reconnects = 0
        self._captures = 0
        self._last_capture_seconds: float | None = None
        self._battery: str | None = None
        self._battery_read_at = 0.0
        self._dial: str | None = None
        self._leaked = False

    # -- capabilities ---------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            live_view=False,
            still_capture=True,
            native_stills=True,
            settings=True,
            video_record=False,
            max_still=self.max_still,
        )

    # -- lifecycle ------------------------------------------------------

    def _connect(self) -> Any:
        """Init a fresh session and check it is the kind we can drive."""
        gp = self._gp
        if gp is None:
            raise CameraError(
                "python-gphoto2 is not installed; the sony backend runs in WSL2 "
                "or on Linux (see sony/wsl-setup.sh)"
            )
        camera = gp.Camera()
        try:
            camera.init()
        except gp.GPhoto2Error as exc:
            raise CameraError(
                f"no camera: {exc}. Is the a6000 on, in PC Remote, and "
                f"attached with `usbipd attach --wsl`?"
            ) from exc

        try:
            model = camera.get_abilities().model
        except gp.GPhoto2Error:
            model = "unknown"
        if self.require_control and "control" not in model.lower():
            try:
                camera.exit()
            finally:
                pass
            raise CameraError(
                f"{model}: not in PC Remote mode. On the camera set "
                f"Setup > USB Connection > PC Remote, then reopen."
            )
        return camera, model

    def _acquire(self) -> None:
        camera, model = self._connect()
        with self._lock:
            self._camera = camera
            self._model = model
            self._last_ok = time.monotonic()
            self._keepalive_failures = 0
            self._leaked = False
        # A first config read proves the link and fills the dial/battery
        # fields so the first /api/status is already honest.
        try:
            self._refresh_info(force=True)
        except CameraError as exc:
            log.warning("sony: opened but the first config read failed: %s", exc)

        stop = threading.Event()
        self._stop = stop
        thread = threading.Thread(
            target=self._keepalive, args=(stop,), name="sony-keepalive", daemon=True
        )
        self._thread = thread
        thread.start()
        log.info("sony: opened %s", model)

    def _release(self) -> None:
        stop = self._stop
        thread = self._thread
        self._thread = None
        stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.join_timeout)
            if thread.is_alive():
                # Same reasoning as GrabThreadBackend: the thread is inside a
                # libgphoto2 call we cannot interrupt, and exit()ing the
                # session under it is a use-after-free. Leak instead.
                self._leaked = True
                log.warning(
                    "sony: keepalive did not exit within %.1fs; leaking the "
                    "session rather than closing it underneath a live call",
                    self.join_timeout,
                )
                return
        self._close_camera()

    def _close_camera(self) -> None:
        with self._lock:
            camera = self._camera
            self._camera = None
        if camera is None:
            return
        try:
            camera.exit()
        except Exception as exc:  # a body that already went away
            log.debug("sony: exit() failed: %s", exc)

    # -- keepalive ------------------------------------------------------

    def _keepalive(self, stop: threading.Event) -> None:
        """Hold the session open and prove it is alive, reconnecting if not.

        Draining the event queue is the poll. It is what Sony's own remote
        software does continuously, it is cheap, and it also stops the queue
        from growing unbounded across a long timelapse.
        """
        while not stop.wait(self.keepalive_interval):
            try:
                with self._lock:
                    camera = self._camera
                    if camera is None:
                        raise CameraError("no session")
                    self._drain_events(camera, timeout_ms=100)
                self._last_ok = time.monotonic()
                self._keepalive_failures = 0
                self._refresh_info()
            except Exception as exc:
                self._keepalive_failures += 1
                self._last_error = f"keepalive: {exc}"
                if self._keepalive_failures == 1:
                    log.warning("sony: keepalive failed: %s", exc)
                self._try_reconnect(stop)

    def _drain_events(self, camera: Any, timeout_ms: int) -> None:
        gp = self._gp
        for _ in range(16):
            ev, _data = camera.wait_for_event(timeout_ms)
            if ev == gp.GP_EVENT_TIMEOUT:
                return

    def _try_reconnect(self, stop: threading.Event) -> None:
        """Drop the dead session and try for a fresh one, once per poll.

        A body that slept re-enumerates as a HID device and comes back as
        the PTP identity only when woken by hand. Until then init() fails
        quickly and we simply try again next interval.
        """
        if stop.is_set():
            return
        self._close_camera()
        try:
            camera, model = self._connect()
        except CameraError as exc:
            self._last_error = f"reconnect: {exc}"
            return
        with self._lock:
            self._camera = camera
            self._model = model
            self._last_ok = time.monotonic()
        self._keepalive_failures = 0
        self._reconnects += 1
        self._last_error = None
        log.info("sony: reconnected to %s", model)

    def _refresh_info(self, force: bool = False) -> None:
        """Dial position and battery, read at most once a minute."""
        now = time.monotonic()
        if not force and now - self._battery_read_at < 60.0:
            return
        cfg = self._get_config()
        self._dial = self._read(cfg, "expprogram")
        self._battery = self._read(cfg, "batterylevel")
        self._battery_read_at = now

    # -- gphoto plumbing ------------------------------------------------

    def _require_camera(self) -> Any:
        camera = self._camera
        if camera is None:
            raise CameraError(self._last_error or "camera is not open")
        return camera

    def _get_config(self) -> Any:
        gp = self._gp
        with self._lock:
            camera = self._require_camera()
            try:
                return camera.get_config()
            except gp.GPhoto2Error as exc:
                self._last_error = f"get_config: {exc}"
                raise CameraError(f"could not read camera config: {exc}") from exc

    @staticmethod
    def _read(cfg: Any, key: str) -> str | None:
        try:
            return str(cfg.get_child_by_name(key).get_value())
        except Exception:
            return None

    @staticmethod
    def _widget(cfg: Any, key: str) -> dict[str, Any]:
        w = cfg.get_child_by_name(key)
        try:
            choices = [str(c) for c in w.get_choices()]
        except Exception:
            choices = []
        return {
            "label": w.get_label(),
            "value": str(w.get_value()),
            "choices": choices,
            "readonly": bool(w.get_readonly()),
        }

    # -- contract -------------------------------------------------------

    def read_frame(self) -> np.ndarray | None:
        return None

    def capture_still(self, path: Path) -> CaptureResult:
        gp = self._gp
        path = Path(path)
        t0 = time.monotonic()
        try:
            with self._lock:
                camera = self._require_camera()
                fp = camera.capture(gp.GP_CAPTURE_IMAGE)
                cf = camera.file_get(fp.folder, fp.name, gp.GP_FILE_TYPE_NORMAL)
                data = bytes(memoryview(cf.get_data_and_size()))
                deleted = True
                try:
                    camera.file_delete(fp.folder, fp.name)
                except gp.GPhoto2Error as exc:
                    # Not fatal: the still is already in hand. But say so,
                    # because a card that fills with undeleted captures is
                    # how a long run ends early.
                    deleted = False
                    log.warning("sony: could not delete %s on camera: %s", fp.name, exc)
            self._last_ok = time.monotonic()
        except CameraError as exc:
            return CaptureResult(ok=False, error=str(exc))
        except Exception as exc:
            self._last_error = f"capture: {exc}"
            return CaptureResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        # Keep the camera's extension when it is not a JPEG (RAW mode), so a
        # .jpg name never wraps an ARW.
        ext = Path(fp.name).suffix.lower()
        if ext and ext not in (".jpg", ".jpeg"):
            path = path.with_suffix(ext)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception as exc:
            return CaptureResult(ok=False, error=f"write failed: {exc}")

        dims = jpeg_dimensions(data) if ext in (".jpg", ".jpeg", "") else None
        elapsed = time.monotonic() - t0
        self._captures += 1
        self._last_capture_seconds = round(elapsed, 2)
        return CaptureResult(
            ok=True,
            path=str(path),
            width=dims[0] if dims else None,
            height=dims[1] if dims else None,
            bytes=len(data),
            meta={
                "backend": self.name,
                "source": "shutter",
                "camera_file": fp.name,
                "deleted_on_camera": deleted,
                "capture_seconds": round(elapsed, 2),
            },
        )

    # -- settings -------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        cfg = self._get_config()
        out: dict[str, Any] = {}
        for key in SETTINGS:
            try:
                out[key] = self._widget(cfg, key)
            except Exception:
                continue  # a widget this firmware does not expose
        self._last_ok = time.monotonic()
        return out

    def set_setting(self, key: str, value: Any) -> str:
        """Write one setting and return the token the camera reports back.

        Sony applies exposure changes by stepping through values on the body,
        so a write is only done when the read-back agrees. Waiting up to
        `settle` for that is what makes the returned value trustworthy.
        """
        gp = self._gp
        if key not in SETTINGS:
            raise CameraError(f"{key!r} is not a settable key; choose from {', '.join(SETTINGS)}")

        with self._lock:
            camera = self._require_camera()
            cfg = self._get_config()
            try:
                w = cfg.get_child_by_name(key)
            except Exception as exc:
                raise CameraError(f"{key!r} is not exposed by this camera") from exc
            info = self._widget(cfg, key)
            if info["readonly"]:
                dial = self._dial or "not M"
                raise CameraError(
                    f"{key} is read-only right now (mode dial: {dial}). "
                    f"Exposure is only writable with the dial on M."
                )
            token = match_choice(value, info["choices"]) if info["choices"] else str(value)
            if token is None:
                raise CameraError(
                    f"{value!r} is not a valid {key}; choices: {', '.join(info['choices'])}"
                )
            if token == info["value"]:
                return token
            try:
                w.set_value(token)
                camera.set_config(cfg)
            except gp.GPhoto2Error as exc:
                self._last_error = f"set {key}: {exc}"
                raise CameraError(f"camera refused {key}={token!r}: {exc}") from exc

            deadline = time.monotonic() + self.settle
            actual = info["value"]
            while time.monotonic() < deadline:
                time.sleep(0.2)
                actual = self._read(self._get_config(), key) or actual
                if actual == token:
                    self._last_ok = time.monotonic()
                    if key == "expprogram":
                        self._dial = actual
                    return actual
            raise CameraError(
                f"camera reports {key}={actual!r} after setting {token!r}; "
                f"the body did not take the value"
            )

    # -- health ---------------------------------------------------------

    @property
    def link_age(self) -> float | None:
        if self._last_ok == 0.0:
            return None
        return time.monotonic() - self._last_ok

    @property
    def healthy(self) -> bool:
        if not self._opened or self._camera is None:
            return False
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        age = self.link_age
        return age is not None and age < self.stale_after

    def status(self) -> dict[str, Any]:
        st = super().status()
        age = self.link_age
        details: dict[str, Any] = {
            "model": self._model,
            "mode dial": self._dial,
            "battery": self._battery,
            "captures": self._captures,
        }
        if self._last_capture_seconds is not None:
            details["last capture"] = f"{self._last_capture_seconds}s"
        if self._reconnects:
            details["reconnects"] = self._reconnects
        st.update(
            {
                "link_age": round(age, 3) if age is not None else None,
                "keepalive_failures": self._keepalive_failures,
                "handle_leaked": self._leaked,
                "details": details,
            }
        )
        if self._dial and self._dial != "M":
            st["warning"] = (
                f"mode dial is on {self._dial}; exposure settings are locked "
                f"until it is turned to M"
            )
        return st
