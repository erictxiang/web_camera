# web_camera

Two cameras, one service, two processes on two hosts — because they share almost nothing
but the interface.

- **[`osmo/`](osmo/PLAN.md)** — DJI Osmo Pocket 3 over USB (UVC). Windows native. Also
  home to the shared `camrig` package, the server and the UI.
- **[`sony/`](sony/README.md)** — Sony a6000 over PTP. Runs the same server from WSL2 with
  the `sony` backend: real shutter, 6000×4000 stills, exposure control in M mode, no
  live view.

Both are built against the constraints recorded in the `camrig Handoff` artifact
(2026-08-21) and the a6000 bench results of 2026-09-04. The docs in each folder exist so
those are not hit twice.

```
osmo\.venv\Scripts\python osmo\server.py --backend osmo --port 8030    # Windows
.\sony\start.ps1                                                       # WSL2, port 8031
```

Publishing on the tailnet is owned by `C:\Users\ericx\home_server` (`settings.psd1`
ExtraApps → `04-publish.ps1`): Osmo on :12000, Sony on :15000.

Start with `osmo/PLAN.md`.
