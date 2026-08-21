# web_camera

Two cameras, two folders, because they share almost nothing but the interface.

- **[`osmo/`](osmo/PLAN.md)** — DJI Osmo Pocket 3 over USB (UVC). Active. Windows native.
- **[`sony/`](sony/README.md)** — Sony a6000 over PTP. Deferred, no hardware yet. WSL2 or Pi.

Both are built against the constraints recorded in the `camrig Handoff` artifact
(2026-08-21). Those constraints were found by hitting them; the docs in each folder exist
so they are not hit twice.

Start with `osmo/PLAN.md`.
