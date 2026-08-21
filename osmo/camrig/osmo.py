"""DJI Osmo Pocket 3 over USB, in UVC webcam mode.

Bring-up notes measured against real hardware on 2026-08-21 (see PLAN.md):

  * index 0 was the camera; 1-7 did not open at all
  * MJPG 1920x1080 negotiated first try and held ~30 fps
  * CAP_PROP_FPS returns -1.0 -- unsupported, use measured throughput
  * CAP_PROP_READ_TIMEOUT_MSEC is rejected outright by DSHOW

Two constraints that are properties of the camera, not bugs to fix:

  * Webcam mode is entered by tapping the Pocket 3's own touchscreen after
    plugging in. There is no USB, Wi-Fi or BLE equivalent, so nothing here can
    recover the device unattended after a power cycle.
  * There is no shutter command on any transport. Every still is a frame
    lifted from the video stream, which is why native_stills is False and
    max_still is the video resolution rather than a sensor resolution.
"""

from __future__ import annotations

import logging
import sys

import cv2
import numpy as np

from .base import Capabilities, CameraError, GrabThreadBackend

log = logging.getLogger(__name__)

MJPG = cv2.VideoWriter_fourcc(*"MJPG")


def fourcc_str(value: float) -> str:
    n = int(value)
    if n <= 0:
        return "----"
    return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4))


class OsmoBackend(GrabThreadBackend):
    name = "osmo"

    def __init__(
        self,
        device: int = 0,
        width: int = 1920,
        height: int = 1080,
        api: int | None = None,
    ) -> None:
        super().__init__()
        self.device = device
        self.width = width
        self.height = height
        # DirectShow on Windows, V4L2 on Linux. WSL2 is not an option at all:
        # its kernel is built without CONFIG_USB_VIDEO_CLASS, so a usbipd
        # attached camera shows up in lsusb and never becomes /dev/video0.
        if api is not None:
            self.api = api
        elif sys.platform == "win32":
            self.api = cv2.CAP_DSHOW
        else:
            self.api = cv2.CAP_V4L2
        self._cap: cv2.VideoCapture | None = None
        self._negotiated: dict[str, object] = {}

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            live_view=True,
            still_capture=True,
            # Stills are video frames. The camera has no shutter to call.
            native_stills=False,
            # No exposure protocol exists for this camera on any transport.
            settings=False,
            # Nothing implements recording yet. It stays false until something
            # does -- the handoff's OsmoBackend declared this true with nothing
            # behind it, and that is worse than not offering it.
            video_record=False,
            max_still=(self.width, self.height),
        )

    def _open_source(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.device, self.api)
        if not cap.isOpened():
            cap.release()
            raise CameraError(
                f"index {self.device} would not open. Is the Pocket 3 plugged in, "
                f"and was 'Webcam' tapped on its touchscreen? There is no way to "
                f"enter webcam mode remotely."
            )

        # fourcc before the dimensions, on purpose: asking for a size first can
        # pin a format that cannot carry it. At 1080p the Pocket 3 must
        # negotiate MJPG -- the YUY2 fallback drops resolution or crawls at
        # single-digit fps.
        cap.set(cv2.CAP_PROP_FOURCC, MJPG)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        got_fourcc = fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
        self._negotiated = {
            "device": self.device,
            "fourcc": got_fourcc,
            "width": got_w,
            "height": got_h,
        }

        # DirectShow accepts settings it does not apply, so log the readback
        # rather than the request, and warn instead of failing -- a smaller
        # frame is still a usable camera.
        if got_fourcc != "MJPG":
            log.warning(
                "osmo: asked for MJPG, got %s -- expect reduced resolution or "
                "single-digit fps",
                got_fourcc,
            )
        if (got_w, got_h) != (self.width, self.height):
            log.warning(
                "osmo: asked for %dx%d, got %dx%d",
                self.width,
                self.height,
                got_w,
                got_h,
            )
            self.width, self.height = got_w, got_h

        self._cap = cap
        return cap

    def _grab(self, source: cv2.VideoCapture) -> np.ndarray | None:
        ok, frame = source.read()
        if not ok or frame is None:
            return None
        return frame

    def _close_source(self, source: cv2.VideoCapture) -> None:
        if self._cap is source:
            self._cap = None
        source.release()

    def status(self) -> dict:
        st = super().status()
        st["negotiated"] = dict(self._negotiated)
        return st


def list_devices(max_index: int = 8, warmup: int = 10) -> list[dict]:
    """Indices that open, and whether each actually delivers a frame.

    Some indices open and never yield anything -- normal DirectShow behaviour,
    not a fault -- so 'opened' and 'delivers' are reported separately.
    """
    import time

    api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2
    found = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            continue
        try:
            frame = None
            for _ in range(warmup):
                ok, candidate = cap.read()
                if ok and candidate is not None:
                    frame = candidate
                    break
                time.sleep(0.1)
            found.append(
                {
                    "index": index,
                    "delivers": frame is not None,
                    "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "fourcc": fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)),
                }
            )
        finally:
            cap.release()
    return found


if __name__ == "__main__":
    for dev in list_devices():
        mark = "DELIVERS" if dev["delivers"] else "opened, no frame"
        print(
            f"index {dev['index']}: {mark}  {dev['fourcc']} "
            f"{dev['width']}x{dev['height']}"
        )
