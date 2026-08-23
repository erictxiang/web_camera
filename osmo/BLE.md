# BLE control — research notes (2026-08-22)

Why this matters: BLE is a separate radio from USB. Everything ruled out so far —
UVC controls, file mode, RNDIS — lives on the cable, where video and control are
mutually exclusive USB configurations. A BLE channel would run *alongside* webcam
mode: clean 1080p UVC over the cable, commands over the air. It is the only path
that adds control without giving up the video feed that already works.

## The landscape (surveyed 2026-08-22)

The handoff's one-liner ("community BLE libraries cover pairing, gimbal telemetry
and stream config — never capture") turns out to be accurate but understated.
Four projects matter:

### lib-osmo-ble (yigitkonur) — the protocol reference
Node.js, Pocket 3 specifically. Built from Wireshark captures of the Mimo app.
The most useful protocol documentation of the four:

- **DUML framing**: `0x55` start byte, 10-bit length, 6-bit version, CRC8
  (poly 0x31, reflected), 16-bit LE target, 16-bit BE sequence, flags, command
  set, command ID, payload, CRC16 (0x1021, reflected) trailer.
- **BLE surface**: service `fff0`; characteristic `fff4` (read/write/notify,
  pairing + inbound), `fff5` (writeWithoutResponse — the channel that actually
  processes commands). `fff3` accepts writes and silently drops them, which
  cost earlier implementations real debugging time.
- **Works**: pairing, telemetry at ~20 Hz, gimbal command *parsing*.
- **Caveat that matters to us**: gimbal motor commands are accepted but
  silently ignored unless Wi-Fi streaming is active. Control may be gated on
  an active streaming session — unknown whether USB webcam mode counts.

### node-osmo (datagutt) — the practical feature list
TypeScript, targets Action 3/4/5 and Pocket 3. Implements pairing, Wi-Fi
connect, resolution / fps / bitrate / stabiliser selection, RTMP stream start,
battery level. Self-described as not fully stable; noble-based BLE behaves
differently per platform. Inspired by the Moblin streaming app's work.

### reverse-engineering-dji + libdji (xaionaro)
Go/C++ side. Got livestreaming working end to end on a Pocket 3 without Mimo
(`djictl`). Ships a **Wireshark dissector for DUML over BLE** — the single most
useful artifact for extending the protocol ourselves — plus notes on pulling
BLE HCI logs off an Android phone with `adb bugreport`.

## What nobody has: camera settings

No public implementation sets exposure, ISO, shutter or EV over BLE. Three
readings of that fact, in decreasing likelihood:

1. Nobody tried — every project so far chased streaming or gimbal control.
2. The commands exist but are gated (like gimbal motors) behind an active
   session type nobody tested.
3. They genuinely are not in the BLE command surface.

The commands *exist* in DUML space — Mimo sets Pro-mode parameters remotely
when connected. Whether they travel over BLE or only over the Wi-Fi link Mimo
establishes after the BLE handshake is exactly the open question. If they are
Wi-Fi-only, the BLE path still matters: it is how the Wi-Fi session gets
bootstrapped.

## What BLE would buy us, worst case to best

| Confidence | Capability | Basis |
|---|---|---|
| High | Battery level, telemetry | Implemented in two projects |
| High | RTMP stream start/config | Implemented in three projects |
| Medium | Gimbal mode (lock / follow / FPV), recenter | Implemented; may need active stream |
| Unknown | **Exposure / ISO / shutter / EV** | Never attempted publicly |
| Ruled out | Photo capture | Handoff: does not exist on any transport |

Even the worst case is not nothing for this rig: battery percentage in
`/api/status`, and gimbal **lock** — the "freeze the gimbal" ask from earlier —
without touching the camera.

## Open questions, in test order

1. **Is BLE alive during USB webcam mode?** Cheapest and most important. If the
   camera stops advertising in webcam mode, the whole idea collapses to
   mode-juggling. Test: BLE scan while streaming over USB.
2. **Does pairing work from Windows?** The PC has a "Generic Bluetooth Radio";
   Python's `bleak` talks WinRT and should see it. Noble-based Node libraries
   are known to be platform-fussy — prefer `bleak` + a port of lib-osmo-ble's
   framing (the DUML spec above is complete enough to reimplement in ~200
   lines).
3. **Do any documented commands work while webcam mode is active?** Battery
   query is the safest probe — read-only, known command, obvious success
   criterion.
4. **Are settings commands discoverable?** The expensive step: Wireshark +
   Android HCI logs while changing exposure in Mimo, using xaionaro's
   dissector. Only worth it if 1–3 pass.

## How it would fit camrig

A `BleControl` sidecar, not a backend: the `osmo` backend keeps owning video
over USB, and a separate module owns the BLE link, surfacing whatever proves
real through the existing capability flags (`settings=True` only for the
subset that measurably works — same rule as everywhere else in this repo:
never claim what the pixels cannot confirm).

Risks, stated once: DUML is undocumented and DJI can change it in any firmware
update; BLE on Windows via a generic radio driver is its own adventure; and the
gimbal-gating caveat suggests DJI gates commands by session type, so results
from one mode do not transfer to another without testing.
