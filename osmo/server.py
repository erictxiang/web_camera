"""camrig server -- REST API, MJPEG preview, and the iPad UI.

    python server.py --backend fake                 # no hardware
    python server.py --backend osmo --device 0      # Windows native, not WSL
    python server.py --backend osmo --host tailscale
    python server.py --backend sony --port 8090 --root-path /sony   # WSL2 only

--host tailscale binds only the tailnet interface, so the rig is reachable from
your other Tailscale devices and from nowhere else on the LAN.

--root-path is for running behind a path on a reverse proxy, which is how both
cameras share one `tailscale serve` site: the Osmo at /osmo on Windows and the
Sony at /sony from WSL2. Routing accepts the request whether or not the proxy
strips the prefix, and every URL the app hands out carries it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi import Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from camrig import BACKENDS, CameraError, TimelapseError, TimelapseRunner, build_backend
from camrig.gate import OwnerGate

log = logging.getLogger("camrig.server")

HERE = Path(__file__).resolve().parent
BOUNDARY = "camrigframe"


class SettingRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=32)
    value: str = Field(..., min_length=1, max_length=64)


class TimelapseRequest(BaseModel):
    """Bad values are rejected as 422 by validation, not by the runner."""

    interval: float = Field(
        ..., ge=0.05, le=3600, description="seconds between frames"
    )
    count: int | None = Field(None, ge=1, le=100_000, description="None runs until stopped")
    name: str | None = Field(None, max_length=48)


def safe_capture_path(root: Path, relative: str) -> Path:
    """Resolve a path inside `root`, or raise 404.

    Anything that escapes -- '..', an absolute path, a symlink pointing out --
    is a 404 rather than a 403, so probing cannot distinguish a blocked path
    from one that does not exist.
    """
    candidate = (root / relative).resolve()
    root = root.resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return candidate


def create_app(
    backend,
    captures_dir: Path,
    stream_fps: float = 15.0,
    open_on_start: bool = True,
    root_path: str = "",
) -> FastAPI:
    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    timelapse = TimelapseRunner(backend, captures_dir)
    root_path = normalize_root_path(root_path)

    stats: dict[str, Any] = {
        "stream_clients": 0,
        "frames_streamed": 0,
        "snapshots": 0,
        "started_at": time.time(),
        "open_error": None,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if open_on_start:
            try:
                backend.open()
            except Exception as exc:
                # Do not take the server down. A camera that is unplugged, or
                # a Pocket 3 that has not had 'Webcam' tapped on its screen,
                # must surface in /api/status where the UI can show it --
                # crashing on boot just hides the reason.
                stats["open_error"] = f"{type(exc).__name__}: {exc}"
                log.error("backend failed to open: %s", exc)
        try:
            yield
        finally:
            try:
                timelapse.stop(timeout=2.0)
            finally:
                backend.close()

    # FastAPI's root_path does two things at once: /docs and the OpenAPI
    # document advertise the prefix, and Starlette's router strips it from an
    # incoming path when present -- so the app answers the same whether the
    # proxy forwards /sony/api/status or /api/status.
    app = FastAPI(title="camrig", version="1.1", lifespan=lifespan, root_path=root_path)

    # -- the owner gate (home_server S3-22, #154) ------------------------
    # Every route is owner-only: the page, the stream, the captures, /api/status
    # (Uptime Kuma's path) and every POST. It wraps the whole app, so a route added
    # later is gated without a line of its own. HOME_OWNER names the owner; unset,
    # everything is refused. See camrig/gate.py.
    app.add_middleware(OwnerGate)

    # -- status ---------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        st = backend.status()
        st["open_error"] = stats["open_error"]
        return {
            "camera": st,
            "timelapse": timelapse.state(),
            "server": {
                "stream_clients": stats["stream_clients"],
                "frames_streamed": stats["frames_streamed"],
                "snapshots": stats["snapshots"],
                "uptime": round(time.time() - stats["started_at"], 1),
                "stream_fps_target": stream_fps,
            },
        }

    @app.post("/api/reopen")
    def reopen() -> dict[str, Any]:
        """Re-acquire the device after a replug.

        The Pocket 3 needs webcam mode tapped on its touchscreen every time it
        powers up, so this exists to let you recover from the UI once you have
        done that, instead of restarting the server.
        """
        if timelapse.running:
            raise HTTPException(409, "stop the timelapse before reopening the camera")
        try:
            backend.close()
            backend.open()
            stats["open_error"] = None
        except Exception as exc:
            stats["open_error"] = f"{type(exc).__name__}: {exc}"
            raise HTTPException(503, stats["open_error"])
        return backend.status()

    # -- live view ------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request, frames: int | None = None):
        """MJPEG preview.

        `frames` bounds the response to N frames and then ends it cleanly.
        Open-ended by default, which is what a browser <img> wants. The bounded
        form exists because an unbounded stream can only be stopped by the
        client hanging up, and that is not something every client can express
        -- scripted grabs and the test suite both need a stream that ends on
        its own.
        """
        if not backend.capabilities.live_view:
            raise HTTPException(400, f"{backend.name} backend has no live view")
        if frames is not None and frames < 1:
            raise HTTPException(422, "frames must be >= 1")

        interval = 1.0 / stream_fps
        budget = frames

        async def frame_stream():
            stats["stream_clients"] += 1
            sent = 0
            try:
                next_at = time.monotonic()
                while budget is None or sent < budget:
                    if await request.is_disconnected():
                        break
                    # Encoding is CPU-bound; keep it off the event loop so one
                    # viewer cannot stall the API for everyone else.
                    jpeg = await asyncio.to_thread(backend.encode_frame)
                    if jpeg is None:
                        await asyncio.sleep(0.1)
                        continue
                    stats["frames_streamed"] += 1
                    sent += 1
                    yield (
                        b"--" + BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n"
                    )
                    next_at += interval
                    await asyncio.sleep(max(0.0, next_at - time.monotonic()))
                if budget is not None:
                    yield b"--" + BOUNDARY.encode() + b"--\r\n"
            finally:
                stats["stream_clients"] -= 1

        return StreamingResponse(
            frame_stream(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.get("/api/frame.jpg")
    async def single_frame():
        """One JPEG, for clients that cannot hold a multipart stream open."""
        jpeg = await asyncio.to_thread(backend.encode_frame)
        if jpeg is None:
            raise HTTPException(503, "no frame available")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    # -- stills ---------------------------------------------------------

    @app.post("/api/snapshot")
    async def snapshot(name: str | None = None) -> dict[str, Any]:
        if not backend.capabilities.still_capture:
            raise HTTPException(400, f"{backend.name} backend cannot capture stills")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        filename = f"{stamp}.jpg" if not name else f"{stamp}-{_slug(name)}.jpg"
        target = captures_dir / filename

        result = await asyncio.to_thread(backend.capture_still, target)
        if not result.ok:
            raise HTTPException(503, result.error or "capture failed")

        stats["snapshots"] += 1
        payload = result.as_dict()
        # The backend may have changed the extension (a RAW capture); name the
        # file the way it actually landed.
        written = Path(result.path).name if result.path else filename
        payload["url"] = f"{root_path}/captures/{written}"
        payload["name"] = written
        return payload

    # -- settings -------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        if not backend.capabilities.settings:
            raise HTTPException(400, f"{backend.name} backend has no settings")
        try:
            settings = await asyncio.to_thread(backend.get_settings)
        except CameraError as exc:
            raise HTTPException(503, str(exc))
        return {"settings": settings}

    @app.post("/api/settings")
    async def set_setting(req: SettingRequest) -> dict[str, Any]:
        """Write one setting. The value returned is what the camera reports
        back, which for Sony is only ever the exact token it accepted."""
        if not backend.capabilities.settings:
            raise HTTPException(400, f"{backend.name} backend has no settings")
        if not backend.opened:
            raise HTTPException(503, backend.last_error or "camera is not open")
        try:
            value = await asyncio.to_thread(backend.set_setting, req.key, req.value)
        except CameraError as exc:
            raise HTTPException(400, str(exc))
        return {"key": req.key, "value": value}

    # -- timelapse ------------------------------------------------------

    @app.get("/api/timelapse")
    def timelapse_state() -> dict[str, Any]:
        return timelapse.state()

    @app.post("/api/timelapse/start")
    def timelapse_start(req: TimelapseRequest) -> dict[str, Any]:
        try:
            return timelapse.start(
                interval=req.interval, count=req.count, name=_slug(req.name)
            )
        except TimelapseError as exc:
            # Already running is a conflict, not a bad request.
            raise HTTPException(409, str(exc))

    @app.post("/api/timelapse/stop")
    def timelapse_stop() -> dict[str, Any]:
        return timelapse.stop()

    # -- captures -------------------------------------------------------

    @app.get("/api/captures")
    def list_captures(limit: int = 60) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        files = sorted(
            (p for p in captures_dir.rglob("*") if p.is_file() and p.suffix.lower() in CAPTURE_TYPES),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        return {
            "captures": [
                {
                    "name": p.name,
                    "url": f"{root_path}/captures/" + p.relative_to(captures_dir).as_posix(),
                    "bytes": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                }
                for p in files
            ]
        }

    @app.get("/captures/{relative:path}")
    def get_capture(relative: str):
        path = safe_capture_path(captures_dir, relative)
        media = CAPTURE_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media)

    # -- UI -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = HERE / "static" / "index.html"
        if not html.is_file():
            raise HTTPException(404, "UI not installed")
        # The page fetches with relative URLs, so one <base> is all it takes
        # to move the whole UI under a proxy prefix.
        text = html.read_text(encoding="utf-8").replace(
            "<head>", f'<head>\n<base href="{root_path}/">', 1
        )
        return HTMLResponse(text)

    return app


#: What the gallery lists and what /captures will serve.
CAPTURE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".arw": "image/x-sony-arw"}


def normalize_root_path(value: str | None) -> str:
    """'' or '/prefix' -- one leading slash, no trailing slash."""
    value = (value or "").strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned[:48] or None


def tailscale_ip() -> str | None:
    """This machine's tailnet address, or None if Tailscale is not usable."""
    candidates = ["tailscale"]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\Tailscale\tailscale.exe")
    for exe in candidates:
        try:
            out = subprocess.run(
                [exe, "ip", "-4"], capture_output=True, text=True, timeout=10
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                ip = line.strip()
                # Tailscale hands out CGNAT space, 100.64.0.0/10.
                if re.fullmatch(r"100\.\d+\.\d+\.\d+", ip):
                    return ip
    return None


def resolve_host(host: str) -> str:
    if host != "tailscale":
        return host
    ip = tailscale_ip()
    if ip is None:
        raise SystemExit(
            "could not find a Tailscale IPv4 address. Is Tailscale running "
            "and logged in? (`tailscale status`)"
        )
    return ip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="fake", choices=list(BACKENDS))
    parser.add_argument("--device", type=int, default=0, help="OpenCV index (osmo)")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; 'tailscale' binds the tailnet IP only, "
        "'0.0.0.0' exposes on every interface",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--root-path",
        default="",
        help="path prefix when served behind a proxy, e.g. /sony",
    )
    parser.add_argument("--stream-fps", type=float, default=15.0)
    parser.add_argument("--captures", default=str(HERE / "captures"))
    parser.add_argument(
        "--keepalive", type=float, default=5.0,
        help="seconds between session polls (sony)",
    )
    parser.add_argument(
        "--fake-settings", action="store_true",
        help="give the fake backend a settings panel, to try the UI without a Sony",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.backend == "osmo" and sys.platform not in ("win32", "linux"):
        log.warning("the osmo backend needs Windows or Linux with a UVC driver")
    if args.backend == "sony" and sys.platform == "win32":
        log.warning("the sony backend needs Linux or WSL2 with python-gphoto2; "
                    "see sony/wsl-setup.sh")

    kwargs: dict[str, Any] = {"device": args.device}
    if args.backend == "osmo":
        kwargs.update(width=args.width, height=args.height)
    elif args.backend == "sony":
        kwargs.update(keepalive=args.keepalive)
    elif args.backend == "fake":
        kwargs.update(settings=args.fake_settings)
    backend = build_backend(args.backend, **kwargs)

    host = resolve_host(args.host)
    app = create_app(
        backend, Path(args.captures), stream_fps=args.stream_fps, root_path=args.root_path
    )
    if args.root_path:
        log.info("serving under %s", normalize_root_path(args.root_path))

    if args.host == "tailscale":
        log.info("bound to the tailnet only -- reachable at http://%s:%d",
                 host, args.port)
    elif host in ("127.0.0.1", "localhost"):
        log.info("bound to localhost only; use --host tailscale to reach it "
                 "from your other devices")

    import uvicorn

    uvicorn.run(app, host=host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
