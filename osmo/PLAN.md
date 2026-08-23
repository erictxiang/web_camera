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
   `release()` frees memory a thread is sitting inside. At shutdown, deliberately leak the
   handle if the grab thread has not exited — that is load-bearing, not a tidiness problem.
   **Note the handoff's other half of this mitigation does not apply here:**
   `CAP_PROP_READ_TIMEOUT_MSEC` is silently rejected by `CAP_DSHOW` (measured — `set()`
   returns `False`, see findings below). It works on the FFMPEG-backed `stream` path, which
   is where the handoff learned it. The UVC path needs a watchdog instead: the grab thread
   stamps a monotonic timestamp after each successful read, and a supervisor marks the
   backend unhealthy when that stamp goes stale. There is no way to unblock the read
   itself.
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

## Hardware findings — 2026-08-21, Pocket 3 attached

Steps 1 and 2 are **done**. Measured, not inferred:

| Finding | Value |
|---|---|
| Windows enumeration | `OsmoPocket3`, Camera class, plus MEDIA + two AudioEndpoints |
| OpenCV index | **0** — the only index that opens; 1–3 do not open at all |
| Default format | 1280×720, fourcc unreported (`----`) |
| After MJPG 1080p request | readback **MJPG 1920×1080**, frames really are `(1080, 1920, 3)` |
| Throughput | **30.5 fps** over 200 frames — comfortably past the ≥25 target |
| `CAP_PROP_FPS` | returns `-1.0`; unsupported by this device, use measured fps |
| `CAP_PROP_READ_TIMEOUT_MSEC` | **`set()` returns `False`** — unsupported on DSHOW |
| `release()` on an idle capture | 0.54 s, clean |
| JPEG encode at q92 | ~103 KB |

The pessimistic expectations did not materialise: there was no phantom index that opens
without delivering, and MJPG negotiation worked first try. Only index 0 exists, so the
"identify the Pocket 3 among other video devices" problem is currently moot — it comes
back the moment a virtual camera is installed.

### Resolved: portrait pillarboxing

The camera was initially in **portrait orientation**, so the 1920×1080 container carried
only a 608×1080 column of image with black bars either side — 31.7% of the pixels. That
matters more here than on a normal camera: the Pocket 3 has no shutter, so stills are
already capped at what the video stream delivers, and 68% bars discards most of that cap.

Rotating the Pocket 3's screen to landscape fixed it. Measured before and after:

| | Portrait | Landscape |
|---|---|---|
| Content region | 608×1080 at x=656 | **1920×1080 at x=0** |
| Frame used | 31.7% | **100.0%** |
| Mean pixel value | 18.7 | 68.7 |
| JPEG at q92 | 103 KB | 264 KB |

So `max_still = (1920, 1080)` is honest, and no crop belongs in the backend. `probe.py`
warns automatically if the bars ever come back — worth watching, because the orientation
lives on the camera and nothing in software pins it.

**Not fixed, and deliberately not fixed in code:** the horizon sits 90° off — the camera
body is physically rotated, so the ceiling is along the right edge. That costs no pixels
and belongs to how the camera is aimed. Do not correct it with a rotation in the backend:
rotating 1920×1080 yields 1080×1920 portrait, which re-creates the pillarbox problem this
section just solved. Level the camera instead if the rig is ever fixed in place.

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

| # | Step | Needs hardware | State |
|---|---|---|---|
| 0 | venv + opencv | no | **done** |
| 1 | `probe.py`, find the index | yes | **done — index 0** |
| 2 | MJPG 1080p negotiation | yes | **done — 30 fps, full-frame** |
| 3 | grab thread + buffer | no | **done — `camrig/base.py`** |
| 4 | backend + `fake` + tests | no | **done — 32 tests green** |
| 5 | server | no | **done — `server.py`** |
| 6 | intervalometer | no | **done — 10 ms drift over 5 frames** |
| 7 | UI | no | **done — `static/index.html`** |

The build is complete and running. See `README.md` for how to run and expose it.

### What changed against the plan during the build

- **The MJPEG stream gained a bounded mode** (`/api/stream?frames=N`). An unbounded
  stream can only be ended by the client hanging up, which `TestClient` cannot express —
  it never sends `http.disconnect`, so the generator looped forever and the suite hung.
  Rather than weaken the endpoint, `frames=N` ends the response cleanly, which the tests
  and scripted grabs both wanted anyway. The unbounded default was then verified against
  real uvicorn: killing a client mid-stream releases the slot and leaves the server
  healthy.
- **The fake backend gained an explicit tear-detection marker.** The first version of the
  concurrency test asserted the green channel was constant per frame, which was never
  true — `cv2.LINE_AA` blends hundreds of intermediate values into the text overlay. A
  solid single-valued block, drawn last, is the real invariant.
- **`POST /api/reopen` was added**, because constraint 2 means the camera has to be
  re-acquired by hand after every power cycle and restarting the server for that is
  needless.

## Exposure and the USB control surface — measured 2026-08-22

**Exposure is not reachable over USB.** This was assumed from the handoff; it is now
measured. `exposure_probe.py` drove every UVC control to both ends of its range and
classified each by whether the *pixels* moved, not by what `set()` returned:

| Verdict | Controls |
|---|---|
| REAL — image changed | **none** |
| PHANTOM — claimed success, no effect | `zoom` |
| ABSENT — rejected outright (`-1`) | exposure, auto_exposure, gain, brightness, contrast, saturation, gamma, sharpness, backlight, auto_wb, wb_temperature, hue, focus |

`zoom` is the reason the probe judges by image content: `set()` returned True, the
readback never left 100, and not a pixel moved. The result is trustworthy because the
scene was static — 28 brightness samples spanning 112.3 to 112.9, a range of 0.6 grey
levels. Pointed at a brightening sky, this test would have been worthless.

So `settings=False` on `OsmoBackend` is correct, and moving to a Raspberry Pi would not
change it — what a camera exposes over USB is a property of its firmware, not the host.
Linux would only *report* the absence more honestly than DirectShow does.

### Why: the camera has two USB identities

Enumerating `VID_2CA3` shows two different product IDs, and they are mutually exclusive
USB configurations:

```
PID_0023  webcam mode          MI_00 Camera (UVC)   MI_02 Media (audio)
PID_0020  data mode            MI_00 RNDIS (USB networking)
                               MI_02 USB Mass Storage (SD card)
                               MI_03..MI_07  five BULK interfaces
```

In webcam mode the camera presents video and audio and **nothing else** — there is no
endpoint to send a command to, which is why every UVC control is absent rather than
merely ignored.

The other mode has an obvious control surface: RNDIS gives the camera an IP address over
the cable, and five bulk interfaces is the shape of a proprietary protocol. That is
presumably how DJI's own software drives it. But `PID_0020` exposes **no Camera-class
device** — no UVC video at all.

The practical consequence: over the cable you can have video, or you can have (probably)
control, but not both. Reverse-engineering the `PID_0020` protocol is tractable in
principle — IP is far friendlier than raw USB — but it is undocumented, it is a project,
and it costs the clean 1080p UVC feed that already works.

### What remains for exposure

1. **The camera's own Pro mode**, set by hand. Open question: does webcam mode honour
   those settings or override them with its own auto-exposure? Testable in seconds — set
   EV compensation down, capture a frame, compare mean brightness.
2. **Optical.** An ND filter to cut light overall, or a graduated ND to hold back the sky.
   This is also the honest answer when a bright sky over darker ground simply exceeds the
   sensor's dynamic range, which no exposure setting fixes.

## Still open

- **Video recording** is declared `video_record=False` and unimplemented — correct per the
  handoff's rule that a capability which lies is worse than one that is absent. Build it or
  leave the flag false.
- **No authentication.** The tailnet is the entire security boundary. That is fine for
  `tailscale serve`, and is exactly why `tailscale funnel` must not be used.
- **Unattended operation is still bounded by the touchscreen.** A Pi rig with a systemd
  unit would survive a crash, but not a camera power cycle. Worth settling before any Pi
  work starts.

## Open questions

- **Where does this eventually run?** The handoff assumes a Raspberry Pi as the deployment
  target with a systemd unit, but the Osmo's touchscreen requirement means a Pi rig still
  needs a human present after every power cycle. Worth deciding before the Pi work starts.
- **Wireless path?** The handoff's `stream` backend takes RTMP from DJI Mimo's livestream
  with 1–3 s latency. Not in this plan; add it only if the cable turns out to be the
  binding constraint.
