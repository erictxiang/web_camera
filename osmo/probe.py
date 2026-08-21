"""Find which OpenCV index is the Pocket 3, and whether it can do MJPG 1080p.

Run this on Windows natively (not WSL) with the camera plugged in and webcam
mode tapped on its touchscreen:

    .venv\\Scripts\\python probe.py

Some indices open but never deliver a frame. That is normal DirectShow
behaviour, not a fault -- the index that actually yields a decodable frame is
the one to pass as --device.
"""

import argparse
import sys
import time

import cv2

MJPG = cv2.VideoWriter_fourcc(*"MJPG")


def fourcc_str(value):
    n = int(value)
    if n <= 0:
        return "----"
    return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4))


def describe(cap):
    return {
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": round(cap.get(cv2.CAP_PROP_FPS), 2),
        "fourcc": fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)),
    }


def probe_index(index, warmup):
    """Open one index and report what it actually delivers."""
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        return None

    try:
        # Discard the first few frames; DirectShow commonly returns garbage or
        # nothing at all until the graph has settled.
        frame = None
        for _ in range(warmup):
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
                break
            time.sleep(0.1)

        info = describe(cap)
        info["index"] = index
        info["delivers"] = frame is not None
        if frame is not None:
            info["frame_shape"] = tuple(frame.shape)
        return info
    finally:
        cap.release()


def report_letterbox(frame, threshold=4):
    """Warn if the frame is mostly black bars.

    A Pocket 3 left in portrait orientation delivers a ~608x1080 column of
    image inside the 1920x1080 container -- about a third of the pixels. It
    is not a decode fault, and the only real fix is rotating the camera's
    screen to landscape.
    """
    import numpy as np

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cols = np.where(gray.mean(axis=0) > threshold)[0]
    rows = np.where(gray.mean(axis=1) > threshold)[0]
    if cols.size == 0 or rows.size == 0:
        print("  content   : frame is entirely black -- lens covered, or no signal")
        return

    w = int(cols.max() - cols.min() + 1)
    h = int(rows.max() - rows.min() + 1)
    used = 100.0 * w * h / (frame.shape[0] * frame.shape[1])
    print(f"  content   : {w}x{h} at x={cols.min()} y={rows.min()} "
          f"-- {used:.1f}% of the frame")

    if used < 95.0:
        print(f"  WARNING: {100 - used:.0f}% of the frame is black bars. If the "
              f"camera is in portrait, rotate its screen to landscape -- stills "
              f"are already capped at the video resolution, so this is throwing "
              f"away most of it.")


def negotiate_1080p(index, frames):
    """Ask for MJPG 1080p, then report what we were actually given.

    fourcc is set before the dimensions on purpose. DirectShow silently accepts
    settings it does not apply, so everything here is read back rather than
    assumed.
    """
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"  index {index}: would not open for negotiation")
        return

    try:
        cap.set(cv2.CAP_PROP_FOURCC, MJPG)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

        info = describe(cap)
        print(f"  requested : MJPG 1920x1080")
        print(f"  readback  : {info['fourcc']} {info['width']}x{info['height']} "
              f"@ {info['fps']} fps")

        # The readback above is a property, and DirectShow will report a size it
        # is not delivering. Trust the pixels instead.
        got = 0
        last = None
        start = time.perf_counter()
        for _ in range(frames):
            ok, frame = cap.read()
            if ok and frame is not None:
                got += 1
                last = frame
        elapsed = time.perf_counter() - start

        rate = got / elapsed if elapsed > 0 else 0.0
        print(f"  measured  : {got}/{frames} frames in {elapsed:.2f}s "
              f"= {rate:.1f} fps")
        if last is not None:
            print(f"  pixels    : {last.shape[1]}x{last.shape[0]} actual frame")

        if info["fourcc"] != "MJPG":
            print("  WARNING: not MJPG -- expect low resolution or single-digit fps")
        if (info["width"], info["height"]) != (1920, 1080):
            print("  WARNING: not 1080p -- the camera refused the requested size")
        if last is not None and (last.shape[1], last.shape[0]) != (info["width"],
                                                                  info["height"]):
            print("  WARNING: delivered pixels disagree with the property readback")
        if last is not None:
            report_letterbox(last)
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-index", type=int, default=8,
                        help="how many indices to walk (default 8)")
    parser.add_argument("--warmup", type=int, default=10,
                        help="read attempts before calling an index dead")
    parser.add_argument("--frames", type=int, default=200,
                        help="frames to time during 1080p negotiation")
    parser.add_argument("--no-negotiate", action="store_true",
                        help="only enumerate; skip the MJPG 1080p test")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("WARNING: UVC capture only works on Windows natively. WSL2's kernel "
              "has no uvcvideo, so an attached camera never becomes /dev/video0.\n")

    print(f"Walking indices 0..{args.max_index - 1} on CAP_DSHOW\n")

    live = []
    for index in range(args.max_index):
        info = probe_index(index, args.warmup)
        if info is None:
            print(f"index {index}: did not open")
            continue
        if info["delivers"]:
            live.append(info)
            print(f"index {index}: DELIVERS  {info['frame_shape']}  "
                  f"{info['fourcc']} {info['width']}x{info['height']} "
                  f"@ {info['fps']} fps")
        else:
            print(f"index {index}: opened but no frame  "
                  f"({info['fourcc']} {info['width']}x{info['height']})")

    print()
    if not live:
        print("No index delivered a frame.")
        print("Check: is the Pocket 3 plugged in, and was 'Webcam' tapped on its")
        print("touchscreen? There is no way to enter webcam mode remotely.")
        return 1

    if len(live) > 1:
        print(f"{len(live)} indices deliver frames: "
              f"{[i['index'] for i in live]} -- another camera or a virtual "
              f"camera is present. Identify the Pocket 3 by resolution.")

    if not args.no_negotiate:
        for info in live:
            print(f"\nNegotiating MJPG 1080p on index {info['index']}:")
            negotiate_1080p(info["index"], args.frames)

        # Measured on a Pocket 3 2026-08-21: DSHOW rejects this outright, so a
        # hung read cannot be bounded from here and the grab thread needs a
        # staleness watchdog instead. Re-checked each run in case a future
        # OpenCV or driver changes the answer.
        probe_cap = cv2.VideoCapture(live[0]["index"], cv2.CAP_DSHOW)
        if probe_cap.isOpened():
            supported = probe_cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
            print(f"\nCAP_PROP_READ_TIMEOUT_MSEC accepted: {bool(supported)}"
                  f"{'' if supported else '  (expected on DSHOW -- use a watchdog)'}")
        probe_cap.release()

    print(f"\nUse: --device {live[0]['index']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
