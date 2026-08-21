# Osmo Pocket 3 — bring-up plan

Target: a Python service on **Windows native** that opens the Pocket 3 in UVC webcam
mode, serves a live MJPEG preview and on-demand JPEG snapshots over HTTP, and runs an
absolute-schedule intervalometer.

Source of truth for the constraints below: the `camrig Handoff` artifact (2026-08-21).
None of that project's code exists on this machine — this is a fresh build that reuses
its findings.

---

## Ground rules (do not rediscover)

1. **Windows native only.** WSL2's kernel lacks `CONFIG_USB_VIDEO_CLASS`, so a
   `usbipd`-attached Pocket 3 shows in `lsusb` and never becomes `/dev/video0`. All Osmo
   work happens in a Windows shell against DirectShow.
2. **Webcam mode needs a finger.** After plugging in, the mode is selected by tapping the
   Pocket 3's own touchscreen. There is no USB / Wi-Fi / BLE equivalent. Nothing in this
   design may assume unattended recovery after a power cycle.
3. **There is no shutter command.** Not over any transport. Every "still" is a frame
   lifted from the video stream, capped at 1080p. Do not go looking for a photo API.
4. **`release()` on a blocked `VideoCapture` segfaults the process.** If a read is hung,
   `release()` frees memory a thread is sitting inside. Bound every read with
   `CAP_PROP_READ_TIMEOUT_MSEC`, and at shutdown deliberately leak the handle if the grab
   thread has not exited. Both are load-bearing, not tidiness problems.
5. **One thread owns the device.** A preview and a timelapse both reading the same
   `cv2.VideoCapture` produces torn frames or a hang. Single grab thread → one-slot
   buffer → every consumer takes a `.copy()` under a lock.

## Environment as verified on this machine (2026-08-21)

| Thing | State |
|---|---|
| Python | 3.12.10 — `C:\Users\ericx\AppData\Local\Programs\Python\Python312\python.exe` |
| `fastapi` | installed (global) |
| `opencv-python` | **not installed** — step 0 |
| Camera-class PnP devices | **none attached** — clean baseline |

The clean baseline matters: with no other camera on the machine, the Pocket 3 should land
at OpenCV index 0. If a virtual camera (OBS, Teams, DroidCam) ever gets installed, that
assumption dies and `probe.py` becomes mandatory rather than convenient.

---

## Step 0 — environment

```
python -m venv .venv
.venv\Scripts\python -m pip install opencv-python numpy fastapi "uvicorn[standard]"
```

Local venv, not the global interpreter — OpenCV's DirectShow backend is the one moving
part here and it should be pinnable independently of whatever else uses global fastapi.

## Step 1 — probe with the camera attached  ← **first thing to do with hardware**

Plug the Pocket 3 in, tap **Webcam** on its screen, then:

```
.venv\Scripts\python probe.py
```

`probe.py` walks indices 0..7 on `CAP_DSHOW`, and for each one that opens, reports
whether a frame actually arrives, plus its resolution, fps and fourcc. Expect **at least
one index that opens but never yields a frame** — that is normal DirectShow behaviour,
not a fault. Record the index that produces real frames; that is the `--device` value.

Acceptance: exactly one index yields a decodable 1920×1080 frame, and re-running after an
unplug/replug gives the same index.

## Step 2 — format negotiation

The fragile part. At 1080p the Pocket 3 must negotiate **MJPG**; the YUY2 fallback will
either drop to a lower resolution or crawl at single-digit fps. Order matters:

```python
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1920)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
```

Set fourcc **before** the dimensions, then read all three back and assert — DirectShow
silently accepts settings it does not apply. Log the readback, never the request.

Acceptance: readback is MJPG / 1920×1080, and a 200-frame timing loop holds ≥25 fps.

## Step 3 — grab thread and one-slot buffer

The concurrency core, and the piece a well-meaning refactor will break.

- One `threading.Thread` runs `cap.read()` in a loop and owns the handle exclusively.
- It writes the newest frame into a single slot under a `Lock`; older frames are dropped.
  No queue — a queue would let a slow consumer add latency to a fast one.
- `read_frame()` takes the lock, returns `frame.copy()`, releases. It never hands out the
  buffer itself.
- Every read is bounded by `CAP_PROP_READ_TIMEOUT_MSEC`. On timeout the thread marks the
  backend unhealthy rather than blocking forever.
- `close()` signals the thread, joins with a timeout, and **if the join times out, returns
  without calling `release()`** — leaking the handle is the correct trade against a
  segfault.

Acceptance: a stress script running a preview loop and a snapshot loop concurrently for 5
minutes produces zero torn frames and zero crashes; 8 open/close cycles in a row leave the
process alive.

## Step 4 — the backend class

Same five-method contract the handoff describes, so the Sony work slots in later against
the same interface:

```
_acquire()        -> None            # open device; raise CameraError on failure
_release()        -> None            # must be safe to call twice
capabilities      -> Capabilities
read_frame()      -> ndarray | None  # BGR copy, or None if no live view
capture_still(p)  -> CaptureResult   # ok/path/width/height/bytes/error/meta
```

Osmo declares:

```
live_view=True  still_capture=True  native_stills=False
settings=False  video_record=False  max_still=(1920, 1080)
```

`native_stills=False` and `video_record=False` are deliberate. The handoff records that
the original `OsmoBackend` declared `video_record=True` with nothing behind it — a
capability that lies is worse than one that is absent. It stays false until something
implements it.

`capture_still()` grabs from the buffer and encodes JPEG. On failure it returns
`CaptureResult(ok=False)` — it does **not** raise, because the timelapse loop must count a
bad frame and keep going rather than lose a 400-frame run.

Build the `fake` backend in this same step. It generates synthetic frames and lets the
whole stack run with no hardware, which is what makes any of this testable in CI.

## Step 5 — server

FastAPI. Minimum surface:

- `GET /api/status` — backend, capabilities, healthy, frames served, last error
- `GET /api/stream` — `multipart/x-mixed-replace` MJPEG, target 15 fps
- `POST /api/snapshot` — one JPEG into `captures/`, returns the CaptureResult
- `GET /captures/{name}` — path-traversal guard (`..` → 404, not 500)
- `POST /api/timelapse/start` / `stop`, `GET /api/timelapse` — double-start → 409, invalid
  interval or count → 422

## Step 6 — intervalometer

Absolute schedule, not `sleep(interval)`. Compute `start + n * interval` and sleep the
remainder, so per-frame error never accumulates. Runs in its own thread and calls
`capture_still()` — it is just another consumer of the buffer.

Acceptance: 6 frames at 0.5 s drift under ~10 ms total; the run hits its target count
exactly and stops itself.

## Step 7 — UI

Single-file `static/index.html`, no build step, sized for iPad. It reads `/api/status` and
**hides controls the backend cannot support** rather than showing buttons that fail — this
is what keeps the same UI honest once the Sony backend, which has no live view at all,
is added.

---

## Tests, written alongside — not after

The handoff's largest single gap was that all verification had been manual. Against the
`fake` backend, with no hardware, in CI:

- interval accuracy under load
- concurrent stream + timelapse
- a capture failure counted without ending the run
- backend open failure surfacing in `/api/status`
- the `/captures/` traversal guard

## Order of work

| # | Step | Needs hardware |
|---|---|---|
| 0 | venv + opencv | no |
| 1 | `probe.py`, find the index | **yes** |
| 2 | MJPG 1080p negotiation | **yes** |
| 3 | grab thread + buffer | no (`fake`) |
| 4 | backend + `fake` + tests | no |
| 5 | server | no |
| 6 | intervalometer | no |
| 7 | UI | no |

Steps 1–2 are the only ones gated on the cable. Everything else is built and tested
against `fake`, so the camera needs to be attached only for the two format-negotiation
sessions and a final end-to-end pass.

## Open questions

- **Where does this eventually run?** The handoff assumes a Raspberry Pi as the deployment
  target with a systemd unit, but the Osmo's touchscreen requirement means a Pi rig still
  needs a human present after every power cycle. Worth deciding before the Pi work starts.
- **Wireless path?** The handoff's `stream` backend takes RTMP from DJI Mimo's livestream
  with 1–3 s latency. Not in this plan; add it only if the cable turns out to be the
  binding constraint.
