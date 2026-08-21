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

        got = 0
        start = time.perf_counter()
        for _ in range(frames):
            ok, frame = cap.read()
            if ok and frame is not None:
                got += 1
        elapsed = time.perf_counter() - start

        rate = got / elapsed if elapsed > 0 else 0.0
        print(f"  measured  : {got}/{frames} frames in {elapsed:.2f}s "
              f"= {rate:.1f} fps")

        if info["fourcc"] != "MJPG":
            print("  WARNING: not MJPG -- expect low resolution or single-digit fps")
        if (info["width"], info["height"]) != (1920, 1080):
            print("  WARNING: not 1080p -- the camera refused the requested size")
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

    print(f"\nUse: --device {live[0]['index']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
