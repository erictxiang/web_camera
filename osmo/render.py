"""Render a timelapse folder into a video.

    python render.py captures/20260821-072251-sky
    python render.py captures/20260821-072251-sky --fps 30 -o sky.mp4
    python render.py captures/... --deflicker --half

Uses ffmpeg when it is on PATH (H.264, plays everywhere) and falls back to
OpenCV's mp4v encoder otherwise. The fallback is fine for review but produces
a larger file that some players and browsers will not open, so ffmpeg is worth
installing if these are going anywhere.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def find_frames(folder: Path) -> list[Path]:
    frames = sorted(folder.glob("frame_*.jpg"))
    if not frames:
        frames = sorted(folder.glob("*.jpg"))
    return frames


def probe(frames: list[Path]) -> tuple[int, int]:
    first = cv2.imread(str(frames[0]))
    if first is None:
        raise SystemExit(f"could not read {frames[0]}")
    h, w = first.shape[:2]
    return w, h


def brightness_series(frames: list[Path], step: int = 1) -> list[float]:
    out = []
    for p in frames[::step]:
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        out.append(float(img.mean()) if img is not None else 0.0)
    return out


def render_ffmpeg(
    frames: list[Path], out: Path, fps: float, width: int | None, crf: int
) -> None:
    """Pipe frames to ffmpeg over stdin.

    Frame files are read and re-encoded rather than passed with -pattern_type,
    so a sequence with gaps in its numbering still renders in order.
    """
    w, h = probe(frames)
    if width:
        h = int(round(h * width / w / 2)) * 2
        w = width

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{w}x{h}", "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        # yuv420p is what makes the result playable in browsers and QuickTime
        # rather than only in VLC.
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None
    try:
        for i, p in enumerate(frames, 1):
            img = cv2.imread(str(p))
            if img is None:
                continue
            if (img.shape[1], img.shape[0]) != (w, h):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            proc.stdin.write(img.tobytes())
            if i % 25 == 0 or i == len(frames):
                print(f"\r  {i}/{len(frames)} frames", end="", flush=True)
    finally:
        proc.stdin.close()
        proc.wait()
    print()


def render_opencv(
    frames: list[Path], out: Path, fps: float, width: int | None
) -> None:
    w, h = probe(frames)
    if width:
        h = int(round(h * width / w / 2)) * 2
        w = width

    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        raise SystemExit("OpenCV could not open a video writer")
    try:
        for i, p in enumerate(frames, 1):
            img = cv2.imread(str(p))
            if img is None:
                continue
            if (img.shape[1], img.shape[0]) != (w, h):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            writer.write(img)
            if i % 25 == 0 or i == len(frames):
                print(f"\r  {i}/{len(frames)} frames", end="", flush=True)
    finally:
        writer.release()
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", help="a timelapse folder under captures/")
    ap.add_argument("-o", "--output", help="output file (default: <folder>.mp4)")
    ap.add_argument("--fps", type=float, default=24.0, help="playback rate")
    ap.add_argument("--width", type=int, help="scale to this width, keeping aspect")
    ap.add_argument("--half", action="store_true", help="shorthand for --width 960")
    ap.add_argument("--crf", type=int, default=20,
                    help="ffmpeg quality, lower is better (18 near-lossless, 28 small)")
    ap.add_argument("--opencv", action="store_true", help="force the OpenCV encoder")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"not a directory: {folder}")

    frames = find_frames(folder)
    if not frames:
        raise SystemExit(f"no .jpg frames in {folder}")

    width = 960 if args.half else args.width
    out = Path(args.output) if args.output else folder.with_suffix(".mp4")

    w, h = probe(frames)
    duration = len(frames) / args.fps
    print(f"{len(frames)} frames, {w}x{h} -> {out.name}")
    print(f"{args.fps:g} fps = {duration:.1f}s of video")

    # Cheap warning rather than a silent bad render: an all-black stretch means
    # the camera was asleep or covered, and no encoder setting will fix it.
    dark = sum(1 for b in brightness_series(frames, step=max(1, len(frames) // 40))
               if b < 2.0)
    if dark:
        print(f"  warning: {dark} of the sampled frames are essentially black")

    use_ffmpeg = shutil.which("ffmpeg") is not None and not args.opencv
    if use_ffmpeg:
        render_ffmpeg(frames, out, args.fps, width, args.crf)
    else:
        if not args.opencv:
            print("  ffmpeg not found; using OpenCV's mp4v encoder "
                  "(larger file, less portable)")
        render_opencv(frames, out, args.fps, width)

    if not out.is_file() or out.stat().st_size == 0:
        raise SystemExit("render produced no output")
    print(f"wrote {out}  ({out.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
