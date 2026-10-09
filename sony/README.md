# Sony a6000 — camrig over PTP

Active as of 2026-09-04. The a6000 runs behind the same camrig service and UI as the Osmo
(`../osmo/`), as a **second process inside WSL2**, because the two cameras cannot share a
host: gphoto2 needs libusb through usbipd, which only exists in WSL, and the Osmo's UVC
path only exists on Windows native. The `sony` backend lives with the rest of camrig in
`../osmo/camrig/sony.py`; this folder holds the WSL setup, the launcher, the probe scripts
and what was measured.

```
bash sony/wsl-setup.sh          # once, inside WSL: apt packages + ~/camrig-venv
.\sony\start.ps1                # from Windows: usbipd auto-attach + server on 127.0.0.1:8031
```

Then https://epc.tailb56b06.ts.net:15000/ on the tailnet, once `home_server` has published
it (see below). Locally it is http://127.0.0.1:8031/ and `/docs` for the API.

## Why it is a separate folder

| | Osmo Pocket 3 | Sony a6000 |
|---|---|---|
| Live view | 1080p UVC, or RTMP over Wi-Fi | none usable over USB |
| Stills | video frames only, ≤1080p | real shutter, 6000×4000 |
| Exposure control | none — no protocol exists | shutter/ISO/aperture/comp/WB, M mode only |
| Unattended | no — touchscreen tap after power cycle | mostly — see *sleep* below |
| Dev host | **Windows native** (WSL has no uvcvideo) | **WSL2 + usbipd**, or a Pi |
| Transport | UVC / DirectShow | PTP via python-gphoto2 |

That split is the whole reason for the backend abstraction: one camera is a video device
with no control surface, the other a control surface with no usable video. Both implement
the same five-method interface, and the UI adapts off the declared capabilities — the
viewport disappears and a settings card appears, from the same `index.html`.

## Measured on the real body (2026-09-04)

Everything the backend assumes was measured first with the scripts here. Re-run them
before trusting a new libgphoto2 or a new firmware.

| Script | What it records |
|---|---|
| `a6000-probe.sh` | versions, detection, which widgets are read-only — **run with the dial on M** |
| `a6000-write-test.sh` | set/get round-trips for shutter, ISO, aperture; restores the originals |
| `a6000-capture-test.sh` | one shutter fire, download, delete, bounded by `timeout 60` |

Results:

- **libgphoto2 2.5.33 (system) and 2.5.34 (bundled in the pip wheel) do not show the
  2.5.31 read-only regression.** With the dial on M every exposure widget is writable and
  writes land: 1/250, f/5.6 and ISO 400 all read back as set.
- **Off the M dial the body itself locks shutter, aperture, ISO and compensation.** That
  is indistinguishable from the regression from the API side. The backend reports the
  dial position in `/api/status` so the UI can say which it is.
- **Capture, download and delete take ~6 s** and produce a 6000×4000 JPEG. No hang on the
  delete step. A failed capture against a missing camera fails in 1 s.
- **ISO auto is spelled `Auto ISO`.** `Auto` fails with "Property not found". The backend
  matches loosely against the choice list (`5.6` finds `f/5.6`) but only ever returns the
  camera's own token, and never guesses between two.
- **Sleep.** The body follows its own power-save timer regardless of the USB session, and
  when it sleeps it re-enumerates as a USB HID device (`054c:0994`) instead of the PTP
  identity (`054c:094e`). Every PTP call then fails until it is woken by hand. The
  keepalive thread in the backend holds one session open and polls it every 5 s, which is
  what Sony's own remote software does to keep the body awake — **whether that is
  sufficient at every timer setting is not yet measured.** The honest test is
  `gphoto2 --wait-event=35m` with the dial on M and the timer at its default; if the body
  is still `(Control)` afterwards, the session is the keepalive. Until then set
  *Power Save Start Time* to 30 min (its maximum) and turn on *USB Power Supply*.
- **WSL2 localhost forwarding works.** A server bound to `0.0.0.0` or `127.0.0.1` inside
  WSL answers on Windows `127.0.0.1`, which is what Tailscale Serve proxies. (A test
  server started with `&` from a one-shot `wsl bash -c` dies with the shell — start it
  detached or via `start.ps1`.)

Camera menu: **Setup → USB Connection → PC Remote**, mode dial on **M**. In MTP mode
gphoto2 detects the body as `(MTP)` and the backend refuses to open it with a message
saying so, since MTP can list files but cannot fire the shutter.

## How the pieces fit

```
Windows                                    WSL2 Ubuntu
  usbipd attach --auto-attach  ──USB/IP──▶  libusb ──▶ python-gphoto2 ──▶ camrig sony backend
  tailscale serve :15000 ──▶ 127.0.0.1:8031 ◀──(localhost forwarding)── uvicorn :8031
```

- `start.ps1` finds the PC Remote identity in `usbipd list`, starts `usbipd attach --wsl
  --auto-attach` hidden, then runs `server.py --backend sony` in WSL. Ctrl+C stops both.
  A sleeping body has no PC Remote identity to attach, so the script says so and starts
  the server anyway; the backend reconnects on its own once the body is woken and
  reattached.
- Captures go to `sony/captures/` on the Windows side, so File Browser can see them.
- The backend keeps a single long-lived session and serialises every libgphoto2 call
  under one lock — the library is not thread-safe, and a capture holds the lock for the
  full 6 s. Settings reads queue behind it rather than tear.
- Only nine settings are reachable through the API (`SETTINGS` in `sony.py`). The config
  tree has ~60 widgets and most are either PTP mirrors or things that must never be
  flipped from a web page (USB mode, format card).

## Publishing on the tailnet

Serve mappings and the landing-page cards are generated by
`C:\Users\ericx\home_server\setup\04-publish.ps1` from `settings.psd1` there — do not
hand-edit `tailscale serve`. The Sony entry is:

```
@{ Name = 'Sony camera'; Description = 'a6000 stills, timelapse and exposure control'
   Port = 8031; ServePort = 15000 }
```

Its own port rather than a path, per that repo's convention: a port is a boundary a
tailnet ACL can withhold from a guest, and this page fires a real shutter. Publishing
requires an **elevated** PowerShell:

```
cd C:\Users\ericx\home_server
.\setup\04-publish.ps1
```

`server.py --root-path /sony` also works if a path mount is ever preferred; the app
routes correctly whether or not the proxy strips the prefix, and every URL it emits
carries it.

## Fallback: GPIO hardware trigger

Still valid if PTP ever proves unreliable: two transistors on a Pi pulling the
multi-terminal focus and shutter lines to ground, ~$25 of parts, no handshake to lose.
Composable with PTP — trigger by GPIO, download by gphoto2. Not built.
