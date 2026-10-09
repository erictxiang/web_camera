# The owner gate

Every route of `server.py` is owner-only (home_server S3-22, issue #154), for the Osmo
instance and the Sony instance alike: they are the same file. The gate is
`camrig/gate.py`, vendored from home_server's `identity.gate()` (commit 9789b2b, by way of
the Agent Harness repo's `harness/gate.py`). `tests/gate_contract.json` is a byte copy of
home_server's `services/_common/gate_contract.json`; `tests/test_gate.py` sends every
fixture to the real app, in-process. When home_server changes the rule, copy it again,
replace the contract unchanged and run the test.

## What it admits

- Through Tailscale Serve, with the owner's login: yes. Another login, no login, or Funnel: 403.
- Straight to `127.0.0.1` (no Serve in front): yes, as the owner.
- A `Host` that is neither loopback nor this PC's tailnet name: 403 (DNS rebinding).
- A write sent from another site: 403. A POST that is not `application/json`: 415.
- With `HOME_OWNER` unset, nothing at all, loopback included: 403 `no-owner`.

The camera page sends JSON on every write (`api()` in `static/index.html`). Anything else
that posts to this server (a script, curl) must send `Content-Type: application/json`.

## HOME_OWNER

The owner's tailnet login (an email address). home_server's `install-camera.ps1` bakes it into the
Camera task. For anything started by hand, set it first:

    $env:HOME_OWNER = 'you@example.com'      # the login in `tailscale status`
    python server.py --backend osmo --port 8030

## The Sony instance (WSL)

The Sony side is not a separate server: `sony\start.ps1` runs this same `server.py` inside WSL
(`--backend sony`), so it is gated by the same middleware. `start.ps1` refuses to start when
`HOME_OWNER` is empty, and forwards it into WSL through `WSLENV` (`HOME_OWNER/u`, appended to
any existing value and restored afterwards; `HOME_TAILNET_NAME/u` too when that is set). The
server needs the tailnet name for its Host rule. In WSL the gate finds it by running Windows'
`tailscale.exe` through interop (`/mnt/c/Program Files/Tailscale/`), or you can give it:
`HOME_TAILNET_NAME=epc.your-tailnet.ts.net`. Started by hand inside WSL it is:

    HOME_OWNER=you@example.com python server.py --backend sony --host 127.0.0.1 --port 8031

If `HOME_OWNER` is missing the Sony page answers 403 `no-owner`, and Uptime Kuma's "Sony
camera" monitor goes DOWN until it is restarted with it.
