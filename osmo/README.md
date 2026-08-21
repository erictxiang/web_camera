# camrig — Osmo Pocket 3

A Python service that drives the Pocket 3 in UVC webcam mode behind one web UI and one
REST API: live MJPEG preview, JPEG snapshots, and an absolute-schedule intervalometer.
Runs on Windows natively and is reachable from your other devices over Tailscale.

```
.venv\Scripts\python server.py --backend fake                    # no hardware
.venv\Scripts\python server.py --backend osmo --device 0         # localhost only
.venv\Scripts\python server.py --backend osmo --host tailscale   # your tailnet
```

Interactive API docs at `/docs`. Captures land in `captures/`, timelapse sequences in
dated subfolders.

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest tests -q
```

The tests need no camera — they run entirely against the `fake` backend, which drives the
same grab thread, buffer and shutdown path as the real one.

## Using the Pocket 3

Plug it in, then **tap Webcam on the camera's own touchscreen**. There is no way to enter
that mode over USB, Wi-Fi or BLE, so this is required after every power cycle — the
**Reopen camera** button in the UI re-acquires the device once you have tapped it, without
restarting the server.

Keep the camera in **landscape**. In portrait it delivers a 608×1080 column inside the
1080p frame — two thirds black bars — and since this camera has no shutter, stills are
already capped at the video resolution. `probe.py` warns if that happens.

```
.venv\Scripts\python probe.py     # which index, what format, how many fps
```

## Reaching it over Tailscale

Two ways. Both keep the rig on your tailnet and off the public internet.

### `tailscale serve` — recommended

Run the server on localhost and let Tailscale terminate TLS in front of it:

```
.venv\Scripts\python server.py --backend osmo
tailscale serve --bg 8080
```

It is then at **https://epc.tailb56b06.ts.net/** from any device signed into your tailnet.
Real HTTPS, no certificate warnings, and no Windows Firewall rule needed — the traffic
arrives through `tailscaled` rather than at a listening socket of its own.

`tailscale serve status` shows what is published; `tailscale serve --https=443 off` stops it.

### `--host tailscale` — direct bind

```
.venv\Scripts\python server.py --backend osmo --host tailscale
```

Binds **only** the tailnet interface (100.119.204.29), so it is reachable at
**http://100.119.204.29:8080** from your tailnet and not from the LAN. Plain HTTP, and
Windows Firewall may need to allow inbound on that port the first time:

```
New-NetFirewallRule -DisplayName "camrig" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8080 -RemoteAddress 100.64.0.0/10
```

That rule restricts the opening to Tailscale's CGNAT range, so it does not also expose the
port to your local network.

### Do not use `tailscale funnel`

Funnel publishes to the **public internet**. This service has no authentication of any
kind — the tailnet is the entire security boundary — so anyone who found the URL could
watch the camera and trigger captures. `serve` is tailnet-only; `funnel` is not.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/status` | backend, capabilities, health, counters, timelapse state |
| POST | `/api/reopen` | re-acquire after a replug; 409 while a timelapse runs |
| GET | `/api/stream` | MJPEG; `?frames=N` bounds it and ends cleanly |
| GET | `/api/frame.jpg` | one JPEG, for clients that cannot hold a stream open |
| POST | `/api/snapshot` | writes to `captures/`; `?name=` optional |
| GET | `/api/captures` | recent files, newest first; `?limit=` |
| GET | `/captures/{path}` | serves a capture; anything escaping the root is 404 |
| GET | `/api/timelapse` | current run state |
| POST | `/api/timelapse/start` | `{interval, count?, name?}`; 409 if running, 422 if invalid |
| POST | `/api/timelapse/stop` | stops and returns final state |

```
curl http://100.119.204.29:8080/api/status
curl -X POST http://100.119.204.29:8080/api/snapshot
curl -X POST http://100.119.204.29:8080/api/timelapse/start \
     -H "Content-Type: application/json" \
     -d '{"interval": 30, "count": 120, "name": "sunset"}'
curl -o burst.mjpg "http://100.119.204.29:8080/api/stream?frames=10"
```

## Layout

```
camrig/
  base.py       CameraBackend ABC, Capabilities, CaptureResult,
                and the grab thread / one-slot buffer every backend shares
  osmo.py       UVC -- DirectShow on Windows, V4L2 on Linux
  fake.py       Synthetic frames; the hardware-free regression harness
  timelapse.py  Absolute-schedule interval runner
server.py       FastAPI app, CLI, MJPEG, Tailscale host resolution
static/index.html   Single-file iPad UI, no build step
probe.py        Device enumeration and format negotiation
tests/          32 tests, no hardware required
```

## Things that will bite you

These are properties of the hardware and the platform, not bugs. `PLAN.md` has the full
account; the short version:

- **WSL2 cannot see this camera.** Its kernel has no `CONFIG_USB_VIDEO_CLASS`, so a
  `usbipd`-attached Pocket 3 appears in `lsusb` and never becomes `/dev/video0`. Windows
  native or a Pi.
- **Webcam mode needs a finger on the touchscreen.** Nothing can automate it. An
  unattended rig cannot recover from a power cycle on its own.
- **There is no shutter command.** Every still is a video frame, so `native_stills` is
  false and `max_still` is the video resolution.
- **`release()` on a blocked read segfaults.** `close()` joins the grab thread and, if the
  join times out, returns without releasing. Leaking the handle beats killing the process.
- **`CAP_PROP_READ_TIMEOUT_MSEC` does not work on DirectShow.** Measured — `set()` returns
  `False`. A blocked UVC read cannot be bounded, so a staleness watchdog reports the
  device unhealthy instead. `probe.py` re-checks this every run in case a driver update
  changes the answer.
