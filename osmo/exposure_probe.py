"""Find out which UVC controls the Pocket 3 actually implements.

    .venv\\Scripts\\python exposure_probe.py

Two separate questions get answered here, and they are not the same one:

  1. Does OpenCV claim the property was set? (cheap, and often a lie)
  2. Do the pixels change? (the only answer that counts)

DirectShow accepts settings it does not apply -- this camera has already been
caught doing exactly that with the fourcc negotiation. So every control that
claims success is then driven to both ends of its range with the mean frame
brightness measured at each, and only a control that moves the image is
reported as real.

The camera must be free: stop the camrig server first, or this will find the
device already in use.
"""

from __future__ import annotations

import argparse
import sys
import time

import cv2
import numpy as np

# (name, property, low, high) -- low/high are probe values, clamped by driver.
CONTROLS = [
    ("auto_exposure",   cv2.CAP_PROP_AUTO_EXPOSURE,   0.25, 0.75),
    ("exposure",        cv2.CAP_PROP_EXPOSURE,        -11, -3),
    ("brightness",      cv2.CAP_PROP_BRIGHTNESS,      0, 255),
    ("contrast",        cv2.CAP_PROP_CONTRAST,        0, 255),
    ("saturation",      cv2.CAP_PROP_SATURATION,      0, 255),
    ("gain",            cv2.CAP_PROP_GAIN,            0, 255),
    ("gamma",           cv2.CAP_PROP_GAMMA,           1, 500),
    ("sharpness",       cv2.CAP_PROP_SHARPNESS,       0, 255),
    ("backlight",       cv2.CAP_PROP_BACKLIGHT,       0, 3),
    ("auto_wb",         cv2.CAP_PROP_AUTO_WB,         0, 1),
    ("wb_temperature",  cv2.CAP_PROP_WB_TEMPERATURE,  2800, 6500),
    ("hue",             cv2.CAP_PROP_HUE,             -180, 180),
    ("zoom",            cv2.CAP_PROP_ZOOM,            0, 100),
    ("focus",           cv2.CAP_PROP_FOCUS,           0, 255),
]


def settle(cap, frames: int = 8) -> np.ndarray | None:
    """Read a few frames so a control change has taken effect, return the last."""
    last = None
    for _ in range(frames):
        ok, frame = cap.read()
        if ok and frame is not None:
            last = frame
    return last


def mean_brightness(frame: np.ndarray | None) -> float:
    if frame is None:
        return float("nan")
    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


def probe_control(cap, name, prop, low, high, settle_frames):
    original = cap.get(prop)
    base = mean_brightness(settle(cap, settle_frames))

    results = {}
    for label, value in (("low", low), ("high", high)):
        accepted = bool(cap.set(prop, value))
        readback = cap.get(prop)
        brightness = mean_brightness(settle(cap, settle_frames))
        results[label] = (accepted, readback, brightness)

    cap.set(prop, original)
    settle(cap, settle_frames)

    lo_acc, lo_rb, lo_b = results["low"]
    hi_acc, hi_rb, hi_b = results["high"]

    claims = lo_acc or hi_acc
    readback_moved = abs(lo_rb - hi_rb) > 1e-6
    # 2.0 grey levels is well above frame-to-frame noise on a static scene but
    # low enough to catch a subtle control.
    pixels_moved = (
        not np.isnan(lo_b) and not np.isnan(hi_b) and abs(lo_b - hi_b) >= 2.0
    )

    return {
        "name": name,
        "original": original,
        "claims": claims,
        "readback_moved": readback_moved,
        "pixels_moved": pixels_moved,
        "low": (lo_rb, lo_b),
        "high": (hi_rb, hi_b),
        "base": base,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--settle", type=int, default=8,
                    help="frames to read after each change")
    args = ap.parse_args(argv)

    api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2
    cap = cv2.VideoCapture(args.device, api)
    if not cap.isOpened():
        print("could not open the camera. Is the camrig server still holding it?")
        print("Stop the server (and the timelapse) first, then re-run.")
        return 1

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    if settle(cap, 15) is None:
        print("camera opened but delivered no frames -- is it in webcam mode?")
        cap.release()
        return 1

    print("Point the camera at a STATIC, evenly lit scene before trusting this.")
    print("A changing scene moves the brightness on its own and will read as a")
    print("working control.\n")
    print(f"{'control':>16} {'claims':>7} {'readback':>9} {'pixels':>7}   detail")
    print("-" * 74)

    real, phantom, absent = [], [], []
    try:
        for name, prop, low, high in CONTROLS:
            r = probe_control(cap, name, prop, low, high, args.settle)
            claims = "yes" if r["claims"] else "no"
            rb = "moved" if r["readback_moved"] else "-"
            px = "MOVED" if r["pixels_moved"] else "-"
            detail = (
                f"{r['low'][0]:g}->{r['low'][1]:.1f}  "
                f"{r['high'][0]:g}->{r['high'][1]:.1f}"
            )
            print(f"{name:>16} {claims:>7} {rb:>9} {px:>7}   {detail}")

            if r["pixels_moved"]:
                real.append(name)
            elif r["claims"] or r["readback_moved"]:
                phantom.append(name)
            else:
                absent.append(name)
    finally:
        cap.release()

    print()
    print(f"REAL      (image changed): {', '.join(real) if real else 'none'}")
    print(f"PHANTOM   (claimed, no effect): {', '.join(phantom) if phantom else 'none'}")
    print(f"ABSENT    (rejected outright): {', '.join(absent) if absent else 'none'}")

    if not real:
        print("\nNo UVC control moved the image. Exposure is not reachable over USB;")
        print("the remaining options are the camera's own Pro-mode settings, or")
        print("optical filtering.")
    else:
        print(f"\n{len(real)} control(s) are real and worth wiring into the API.")

    if sys.platform != "win32":
        print("\nOn Linux, cross-check with:  v4l2-ctl -d /dev/video0 --list-ctrls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
