"""The owner gate: every route of the camera server, owner-only (home_server S3-22, #154).

VENDORED, not imported (home_server spec 3 D12: this repo does not import home_server
code). The rule is a port of `identity.gate()` with kind "owner", and of the Host rule's
tailnet-name lookup in `tailnet.py`, both from home_server `services/_common/` at commit
9789b2b88a7ef588014126a4cf084c1b62afd8c4 (2026-10-07; unchanged through 47def58), by way
of the Agent Harness repo's harness/gate.py (branch t/153-gate, 4959e93, S3-21), which
this file is adapted from. When home_server changes the rule, copy it again, replace
tests/gate_contract.json with its `gate_contract.json` unchanged, and run
tests/test_gate.py: that test is how drift shows.

THE REQUEST KINDS. The server listens on 127.0.0.1 and Tailscale Serve proxies to it. The
Osmo server runs on Windows; the Sony instance is the same file run inside WSL, and Serve
still reaches it through the Windows loopback, so it sees the same requests.

  through Serve, with a login  -> that login; it must be the owner. Serve sets
                                  Tailscale-User-Login itself and strips a client's.
  straight to 127.0.0.1        -> the owner (no X-Forwarded-For, no login): a process
                                  on the owner's PC could read the files anyway.
  through Serve, no login      -> Funnel or a tagged device: refused.

A login wins wherever it appears; X-Forwarded-For is tested by presence, not value.

THE RULES, in order, each a 403 (a POST that is not JSON is a 415):
  host        the Host must be a loopback name or this PC's tailnet name: a DNS
              rebinding page names another host.
  cross-site  a request that is not GET or HEAD with Sec-Fetch-Site cross-site or
              same-site (ts.net is a public suffix, so epc's other ports are
              same-site, not same-origin).
  json        a POST must be application/json (a form cannot send it cross-site). The
              camera page's own fetch() sends it (static/index.html, api()).
  funnel      Tailscale-Funnel-Request present.
  anonymous / owner / no-owner   the identity rules above. HOME_OWNER unset means
              nobody is the owner, loopback included: it fails closed.

THE OWNER comes from HOME_OWNER (the owner's tailnet login). home_server's install-camera
bakes it into the Camera task. The Sony is started by hand (sony/start.ps1) and must be
given it: see GATE.md.

THE TAILNET NAME is looked up from `tailscale status --json`. In WSL there is no
tailscale on the PATH, so the lookup also tries Windows' tailscale.exe through WSL
interop, and HOME_TAILNET_NAME, when set, is used as it is (no lookup). Neither changes
the rule: while no name is known a non-loopback Host is refused.

There is no route the gate exempts: the stream, a single frame, the captures, the page
and /api/status (the Uptime Kuma monitor's path) are all gated. Kuma reaches /api/status
through the Serve URL, where Serve credits WSL as the owner, with the tailnet name as Host
and no Sec-Fetch header, so it passes as the owner.

It is a pure ASGI middleware, not Starlette's BaseHTTPMiddleware, which would buffer the
MJPEG stream. Standard library only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import NamedTuple

log = logging.getLogger("gate")

LOGIN_HEADER = "Tailscale-User-Login"
PROXIED_HEADER = "X-Forwarded-For"
FUNNEL_HEADER = "Tailscale-Funnel-Request"
FETCH_SITE_HEADER = "Sec-Fetch-Site"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
SAFE_METHODS = frozenset({"GET", "HEAD"})
FOREIGN_SITES = frozenset({"cross-site", "same-site"})
JSON_TYPE = "application/json"
# GETs that start an action, matched on the path exactly. The camera has none: every
# action is a POST. (The harness has /chat; a new GET that acts is listed here.)
ACTION_GETS: frozenset[str] = frozenset()
LOG_EVERY = 60.0

REASONS = {
    "host": "host: the Host is not a name of this PC",
    "cross-site": "cross-site: a request that acts, sent on behalf of another site",
    "json": "json: a POST must be application/json",
    "funnel": "funnel: a Funnel request is anonymous",
    "anonymous": "anonymous: a request through Serve with no login",
    "owner": "owner: only the owner may use this",
    "no-owner": "no-owner: this server has no owner configured (HOME_OWNER)",
    "websocket": "websocket: this app has no websocket",
}


class Refusal(NamedTuple):
    status: int
    rule: str
    reason: str
    detail: str = ""


class Admission(NamedTuple):
    login: str
    local: bool


def _refused(rule: str, detail: str = "") -> Refusal:
    return Refusal(415 if rule == "json" else 403, rule, REASONS[rule], detail)


# --- the tailnet name (tailnet.py, reduced) ----------------------------------------------

CREATE_NO_WINDOW = 0x08000000
LOOKUP_TIMEOUT = 5.0
TAILSCALE = os.path.join(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files",
                         "Tailscale", "tailscale.exe")
# Inside WSL the Windows install is reachable through interop, at this path.
TAILSCALE_WSL = "/mnt/c/Program Files/Tailscale/tailscale.exe"


def parse_dns_name(stdout) -> str | None:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    name = ((json.loads(stdout).get("Self") or {}).get("DNSName") or "").strip().rstrip(".").lower()
    return name or None


class TailnetName:
    """This PC's tailnet name from `tailscale status --json`: the first success is
    cached, a failure never is (so the next request looks again), and while there is
    no name a non-loopback Host is refused. Call it from a worker thread."""

    def __init__(self, path: str = TAILSCALE, timeout: float = LOOKUP_TIMEOUT):
        self._path, self._timeout = path, timeout
        self._lock = threading.Lock()      # one lookup at a time
        self._name: str | None = None
        self.last_error = ""

    def __call__(self) -> str | None:
        if self._name:
            return self._name
        with self._lock:
            if self._name:
                return self._name
            try:
                proc = subprocess.Popen([self._path, "status", "--json"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                try:
                    out, _ = proc.communicate(timeout=self._timeout)
                except BaseException:
                    proc.kill()
                    try:
                        proc.communicate(timeout=1.0)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                if proc.returncode != 0:
                    self.last_error = f"tailscale status exited {proc.returncode}"
                else:
                    self._name = parse_dns_name(out)
                    if not self._name:
                        self.last_error = "tailscale status has no Self.DNSName"
            except Exception as exc:  # noqa: BLE001 - any failure is "no name yet"
                self.last_error = f"{type(exc).__name__}: {exc}"
            return self._name

    def pin(self, name: str) -> None:
        self._name = name.strip().rstrip(".").lower() or None


tailnet_name = TailnetName(TAILSCALE if sys.platform == "win32" else TAILSCALE_WSL)
if os.environ.get("HOME_TAILNET_NAME", "").strip():
    tailnet_name.pin(os.environ["HOME_TAILNET_NAME"])


# --- the rule (identity.gate, kind "owner") ------------------------------------------------

class Headers:
    """ASGI raw headers with the two methods the rule needs."""

    def __init__(self, raw):
        self._items = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in raw]

    def get(self, name: str, default=None):
        want = name.lower()
        for k, v in self._items:
            if k == want:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        want = name.lower()
        return [v for k, v in self._items if k == want]


def host_name(value: str) -> str | None:
    """The name in a Host header, lower-cased and without its port, or None when the
    value is not a well-formed host[:port]."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        name, rest = host[:end + 1], host[end + 1:]
    else:
        name, colon, port = host.partition(":")
        rest = colon + port
    if rest:
        port = rest[1:]
        if rest[0] != ":" or (port and not (port.isascii() and port.isdigit())):
            return None
    return name or None


def is_owner(login: str | None, owner: str) -> bool:
    return login is not None and bool(owner) and login.lower() == owner.lower()


def _host_rule(headers: Headers, lookup) -> Refusal | None:
    raw = headers.get("Host")
    if raw is None:
        return _refused("host", "no Host header")
    if len(headers.get_all("Host")) > 1:
        return _refused("host", "more than one Host header")
    quoted = f"Host {raw[:200]!r}"
    name = host_name(raw)
    if name is None:
        return _refused("host", f"{quoted} is malformed")
    if name in LOOPBACK_HOSTS:
        return None
    try:
        ours = lookup()
    except Exception:  # noqa: BLE001
        ours = None
    if not ours:
        why = getattr(lookup, "last_error", "")
        return _refused("host", f"{quoted}; the tailnet name is not known yet" + (f" ({why})" if why else ""))
    if name != ours.lower():
        return _refused("host", quoted)
    return None


def gate(method: str, path: str, headers: Headers, owner: str, *, lookup=tailnet_name) -> Admission | Refusal:
    """Admit or refuse one request. `owner` is the owner's tailnet login (HOME_OWNER)."""
    method = (method or "").upper()

    refusal = _host_rule(headers, lookup)
    if refusal is not None:
        return refusal

    site = (headers.get(FETCH_SITE_HEADER) or "").strip().lower()
    if method not in SAFE_METHODS:
        if site in FOREIGN_SITES:
            return _refused("cross-site", f"{method} with Sec-Fetch-Site {site}")
    elif path in ACTION_GETS and site in FOREIGN_SITES:
        return _refused("cross-site", f"GET {path} with Sec-Fetch-Site {site}")

    if method == "POST":
        media = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != JSON_TYPE:
            return _refused("json", f"Content-Type {media!r}" if media else "no Content-Type")

    if headers.get(FUNNEL_HEADER) is not None:
        return _refused("funnel")

    login = (headers.get(LOGIN_HEADER) or "").strip()
    proxied = headers.get(PROXIED_HEADER) is not None
    local = not login and not proxied
    if local and not owner:
        return _refused("no-owner", "a local request, and no owner to credit it to")
    if login:
        who = login
    elif proxied:
        return _refused("anonymous", "X-Forwarded-For and no login")
    else:
        who = owner
    if not is_owner(who, owner):
        return _refused("owner" if owner else "no-owner", f"login {who!r}")
    return Admission(who, local)


# --- the ASGI middleware ---------------------------------------------------------------------

class _RefusalLog:
    """At most one line per rule per LOG_EVERY seconds."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._held: dict[str, int] = {}

    def __call__(self, rule: str, line: str) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(rule)
            if last is not None and now - last < LOG_EVERY:
                self._held[rule] = self._held.get(rule, 0) + 1
                return
            self._last[rule] = now
            held = self._held.pop(rule, 0)
        log.warning(line + (f" (and {held} more by this rule since its last line)" if held else ""))


class OwnerGate:
    """ASGI middleware: every http request goes through gate() first.

    The owner is read from HOME_OWNER on each request (the task bakes it in), so a
    test can set it after import. A websocket is refused: the app has none."""

    def __init__(self, app, owner=None, lookup=None):
        self.app = app
        self._owner = owner
        self._lookup = lookup
        self._log = _RefusalLog()

    def owner(self) -> str:
        return (os.environ.get("HOME_OWNER", "") if self._owner is None else self._owner).strip()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind == "http":
            headers = Headers(scope.get("headers") or [])
            path = scope.get("path", "")
            # The lookup may run tailscale.exe: off the event loop, so a slow one
            # stalls this request and not the SSE streams in flight.
            result = await asyncio.to_thread(
                gate, scope["method"], path, headers, self.owner(), lookup=self._lookup or tailnet_name)
            if isinstance(result, Admission):
                await self.app(scope, receive, send)
                return
            via = "via Serve" if headers.get(PROXIED_HEADER) is not None else "direct"
            line = f"gate refused {scope['method']} {path[:200]!r} ({via}): {result.status} {result.reason}"
            if result.detail:
                line += f" [{result.detail}]"
            self._log(result.rule, line)
            body = result.reason.encode("utf-8")
            await send({"type": "http.response.start", "status": result.status, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        if kind == "websocket":
            self._log("websocket", f"gate refused a websocket on {scope.get('path', '')[:200]!r}")
            await receive()                 # the connect message
            await send({"type": "websocket.close", "code": 1008})
            return
        # An unknown scope type is not served.
        raise RuntimeError(f"owner gate: unhandled ASGI scope type {kind!r}")
