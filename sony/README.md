# Sony a6000 — deferred

Not started. Osmo first (`../osmo/PLAN.md`); this folder is a placeholder holding the
constraints so they are not rediscovered when the a6000 arrives.

## Why it is a separate folder

The two cameras are opposites, and they do not even share a dev host:

| | Osmo Pocket 3 | Sony a6000 |
|---|---|---|
| Live view | 1080p UVC, or RTMP over Wi-Fi | none usable over USB |
| Stills | video frames only, ≤1080p | real shutter, 24 MP |
| Exposure control | none — no protocol exists | shutter/ISO/aperture/comp, M mode only |
| Unattended | no — touchscreen tap after power cycle | yes |
| Dev host | **Windows native** (WSL has no uvcvideo) | **WSL2 + usbipd**, or a Pi |
| Transport | UVC / DirectShow | PTP via gphoto2 |

That split is the whole reason for a backend abstraction: one camera is a video device
with no control surface, the other a control surface with no usable video. Both implement
the same five-method interface from `../osmo/PLAN.md` step 4, and the UI adapts off the
declared capabilities rather than special-casing.

## Known before we start

- **libgphoto2 2.5.31 regressed a6000 support** — config widgets come back read-only and
  captures hang after writing the file. 2.5.27.1 was reported working. A read-only
  `--list-config` is this bug, not a wiring fault. Record the version that behaves on
  first connection and pin it.
- **WSL2 works here**, unlike for the Osmo: gphoto2 reaches PTP cameras through libusb and
  usbfs, needing no kernel driver, so `usbipd attach` is sufficient.
- Camera settings: **USB Connection → PC Remote**, mode dial to **M**. Exposure control
  only exists in M.
- The body's own Wi-Fi is app-only and not scriptable — ignore it.
- A **GPIO hardware trigger** is the fallback that sidesteps PTP entirely: two transistors
  on a Pi pulling the multi-terminal focus and shutter lines to ground, ~$25 of parts. No
  handshake to lose. Composable with PTP — trigger by GPIO, download by gphoto2.

## First step when hardware exists

Write and run an `a6000-probe.sh` that records, before any code is trusted: the
libgphoto2 version, whether config widgets are writable, and whether capture hangs on the
delete step. Every timeout in a Sony backend should come from those numbers rather than
from a guess.
