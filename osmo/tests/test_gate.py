"""The owner gate against home_server's gate contract, in-process (S3-22, #154).

    python -m pytest tests/test_gate.py

tests/gate_contract.json is a byte copy of home_server's services/_common/gate_contract.json
(spec 3 D12); replace it, unchanged, when that file changes. Every fixture is sent to the
real app (server.create_app, gate and all) as a hand-built ASGI request, so the headers are
exactly the fixture's and no client library adds a Host. The app uses the fake backend, a
temporary captures folder and no lifespan: no camera, no device, no socket. HOME_OWNER and
the tailnet name are pinned to the contract's.

Beyond the contract it checks this server's own facts: every route the app has is gated
(found by walking app.routes), /api/status answers the Uptime Kuma monitor's request, the
camera page's writes are JSON, a form POST is refused, and no HOME_OWNER refuses everything.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

CONTRACT = json.loads((HERE / "gate_contract.json").read_text(encoding="utf-8"))
NAMES = CONTRACT["names"]

from camrig import FakeBackend  # noqa: E402
from camrig import gate as gate_mod  # noqa: E402
from server import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setenv("HOME_OWNER", NAMES["owner"])
    monkeypatch.setattr(gate_mod.tailnet_name, "_name", NAMES["tailnet"])


@pytest.fixture
def app(tmp_path):
    return create_app(FakeBackend(width=160, height=120, fps=60), tmp_path / "captures", open_on_start=False)


def fill(headers: dict[str, str]) -> dict[str, str]:
    return {k: v.replace("{owner}", NAMES["owner"]).replace("{guest}", NAMES["guest"])
               .replace("{tailnet}", NAMES["tailnet"]) for k, v in headers.items()}


def send(app, method: str, path: str, headers: dict[str, str], body: bytes = b""):
    """One request straight to the app; (status, body)."""
    async def go():
        raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
        if body or method in ("POST", "PUT", "PATCH"):
            raw.append((b"content-length", str(len(body)).encode()))
        pathname, _, query = path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": pathname, "raw_path": pathname.encode(), "query_string": query.encode(),
                 "root_path": "", "headers": raw, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8030)}
        sent = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if sent:
                return sent.pop(0)
            await asyncio.sleep(3600)

        out = {"status": None, "body": b""}
        done = asyncio.Event()

        async def snd(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    done.set()

        task = asyncio.create_task(app(scope, receive, snd))
        try:
            await asyncio.wait_for(done.wait(), 10)
        finally:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        return out["status"], out["body"]
    return asyncio.run(go())


# The row: one safe GET (the Kuma monitor's path) and one write that touches no device.
GET_PATH = "/api/status"
WRITE = ("POST", "/api/timelapse/stop")
OTHER_WRITE_METHODS = [m for m in CONTRACT["write_methods"] if m != WRITE[0]]
KIND = "owner"


def expected_for(expected, method: str):
    if isinstance(expected, dict):
        return expected.get(method, expected.get("*"))
    return expected


def judge(status: int, expected, is_write: bool) -> bool:
    if expected == "admit":
        return (status not in (403, 415, 501) and status < 500) if is_write else status == 200
    if expected == "not-2xx":
        return not (200 <= status < 300)
    return status == expected


@pytest.mark.parametrize("name", list(CONTRACT["fixtures"]))
def test_contract_fixture(app, name):
    fx = CONTRACT["fixtures"][name]
    expected = CONTRACT["expected"][KIND][name]
    headers = fill(fx["headers"])
    if fx["applies"] == "get+write":
        status, _ = send(app, "GET", GET_PATH, headers)
        assert judge(status, expected_for(expected, "GET"), False), f"GET {GET_PATH}: {status} vs {expected}"
    h = dict(headers)
    if fx.get("body") == "form":
        h["Content-Type"] = "application/x-www-form-urlencoded"
        data = fx["body_text"].encode()
    else:
        h["Content-Type"] = "application/json"
        data = b"{}"
    status, _ = send(app, WRITE[0], WRITE[1], h, data)
    assert judge(status, expected_for(expected, WRITE[0]), True), f"{WRITE}: {status} vs {expected}"


@pytest.mark.parametrize("method", OTHER_WRITE_METHODS)
def test_other_write_methods_are_not_2xx(app, method):
    h = dict(fill(CONTRACT["fixtures"]["owner"]["headers"]), **{"Content-Type": "application/json"})
    status, _ = send(app, method, GET_PATH, h, b"{}")
    assert not 200 <= status < 300


OWNER = fill(CONTRACT["fixtures"]["owner"]["headers"])
LOCAL = fill(CONTRACT["fixtures"]["local"]["headers"])


def walk(routes, prefix=""):
    for r in routes:
        path = prefix + getattr(r, "path", "")
        sub = getattr(r, "routes", None)
        if sub:
            yield from walk(sub, path)
        elif path:
            yield path, sorted(getattr(r, "methods", None) or ["GET"])


# Every route the app registers, the Osmo ones and the Sony ones (/api/settings) alike. The
# list is exact: a route added to server.py fails test_the_route_table_is_exact until it is
# listed here, and test_every_route_is_gated then proves the new row is refused to everyone
# but the owner. FastAPI's own /docs, /redoc and /openapi.json are in the table on purpose.
ROUTES = {
    ("GET", "/"), ("GET", "/api/status"), ("POST", "/api/reopen"), ("GET", "/api/stream"),
    ("GET", "/api/frame.jpg"), ("POST", "/api/snapshot"), ("GET", "/api/settings"),
    ("POST", "/api/settings"), ("GET", "/api/timelapse"), ("POST", "/api/timelapse/start"),
    ("POST", "/api/timelapse/stop"), ("GET", "/api/captures"), ("GET", "/captures/{relative:path}"),
    ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"),
}


@pytest.fixture(params=["", "/sony"])
def any_app(request, tmp_path):
    """The Osmo instance (no root path) and the Sony instance (behind /sony): same file."""
    return create_app(FakeBackend(width=160, height=120, fps=60), tmp_path / "captures",
                      open_on_start=False, root_path=request.param)


def test_the_route_table_is_exact(any_app):
    found = {(m, p) for p, methods in walk(any_app.routes) for m in methods if m != "HEAD"}
    assert found == ROUTES, (sorted(found - ROUTES), sorted(ROUTES - found))


def test_the_gate_is_the_outermost_middleware(any_app):
    assert [m.cls for m in any_app.user_middleware][0] is gate_mod.OwnerGate


def test_every_route_is_gated(any_app):
    app = any_app
    found = list(walk(app.routes))
    paths = {p for p, _ in found}
    assert {"/", "/api/status", "/api/stream", "/api/frame.jpg", "/api/snapshot", "/api/reopen",
            "/api/settings", "/api/timelapse", "/api/timelapse/start", "/api/timelapse/stop",
            "/api/captures"} <= paths, paths
    for path, methods in found:
        concrete = path.replace("{relative:path}", "x.jpg")
        assert "{" not in concrete, f"name the parameter of {path} here"
        for method in methods:
            if method == "HEAD":
                continue
            for who, hdrs in (("no headers", {}), ("anonymous", fill(CONTRACT["fixtures"]["anonymous"]["headers"])),
                              ("guest", fill(CONTRACT["fixtures"]["guest"]["headers"])),
                              ("funnel", fill(CONTRACT["fixtures"]["funnel"]["headers"])),
                              ("rebinding", fill(CONTRACT["fixtures"]["rebind_alone"]["headers"]))):
                h = dict(hdrs)
                if method == "POST":
                    h["Content-Type"] = "application/json"
                status, _ = send(app, method, concrete, h, b"{}" if method == "POST" else b"")
                assert status in (403, 415), f"{method} {path} for {who}: {status}"


def test_docs_and_unknown_paths_are_refused(app):
    for path in ("/docs", "/redoc", "/openapi.json", "/no/such/route", "/captures/../server.py"):
        assert send(app, "GET", path, {})[0] == 403, path


def test_the_owner_gets_the_page_and_the_api(app):
    for path in ("/", "/api/status", "/api/captures", "/api/timelapse"):
        for who in (OWNER, LOCAL):
            assert send(app, "GET", path, who)[0] == 200, (path, who)


def test_the_pages_writes_are_json(app):
    """Every write the page makes is admitted for the owner as JSON, and is a 415 without."""
    for path in ("/api/snapshot", "/api/reopen", "/api/timelapse/stop"):
        status, _ = send(app, "POST", path, dict(OWNER, **{"Content-Type": "application/json"}), b"{}")
        assert status not in (403, 415), (path, status)
        status, body = send(app, "POST", path, dict(OWNER))
        assert status == 415 and b"json" in body, (path, status)
    status, _ = send(app, "POST", "/api/timelapse/start", dict(OWNER, **{"Content-Type": "application/json"}),
                     json.dumps({"interval": 1, "count": 1}).encode())
    assert status not in (403, 415)
    # the Sony control: a JSON write is admitted (the fake backend has no settings, so 400), form is not
    h = dict(OWNER, **{"Content-Type": "application/json"})
    status, _ = send(app, "POST", "/api/settings", h, json.dumps({"key": "iso", "value": "100"}).encode())
    assert status not in (403, 415)
    assert send(app, "POST", "/api/settings", dict(OWNER, **{"Content-Type": "text/plain"}), b"key=iso")[0] == 415


def test_the_page_sends_json_on_every_fetch():
    """The page has one fetch(), inside api(), and api() adds the JSON content type
    to every non-GET call (a static check of the page the owner's phone loads)."""
    html = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert len(re.findall(r"\bfetch\(", html)) == 1
    api = html[html.index("async function api("):]
    api = api[:api.index("const res = await fetch(")]
    assert '"Content-Type": "application/json"' in api and 'options.body = "{}"' in api
    assert 'method !== "GET"' in api


def test_uptime_kuma_monitor_request_is_admitted(app):
    """Kuma reaches /api/status from WSL through Serve, credited as the owner, the
    tailnet name as Host (with the Serve port when the URL has one), no Sec-Fetch-*."""
    kuma = {"Host": NAMES["tailnet"] + ":8443", "X-Forwarded-For": "172.20.0.1",
            "Tailscale-User-Login": NAMES["owner"], "Tailscale-User-Name": "Owner",
            "User-Agent": "Uptime-Kuma/2.0", "Accept": "text/html,*/*", "Connection": "close"}
    assert send(app, "GET", "/api/status", kuma)[0] == 200
    assert send(app, "GET", "/api/status", dict(kuma, **{"Tailscale-User-Login": NAMES["owner"].upper()}))[0] == 200


def test_no_owner_refuses_everything(app, monkeypatch):
    monkeypatch.delenv("HOME_OWNER")
    status, body = send(app, "GET", "/api/status", LOCAL)
    assert status == 403 and b"no-owner" in body
    assert send(app, "GET", "/api/status", OWNER)[0] == 403


def test_refusal_reflects_nothing_and_two_hosts_are_refused(app):
    status, body = send(app, "GET", "/api/status", {"Host": "<script>alert(1)</script>"})
    assert status == 403 and b"script" not in body
    raw = gate_mod.Headers([(b"host", b"127.0.0.1"), (b"host", b"evil.example")])
    verdict = gate_mod.gate("GET", "/api/status", raw, NAMES["owner"], lookup=gate_mod.tailnet_name)
    assert not isinstance(verdict, gate_mod.Admission) and verdict.rule == "host"


def test_websocket_is_closed(app):
    sent = []

    async def go():
        async def receive():
            return {"type": "websocket.connect"}

        async def snd(msg):
            sent.append(msg)

        await app({"type": "websocket", "path": "/ws", "headers": [(b"host", b"127.0.0.1")]}, receive, snd)

    asyncio.run(go())
    assert sent and sent[0]["type"] == "websocket.close"


def test_a_lookup_that_fails_refuses_a_tailnet_host_but_not_loopback(app, monkeypatch):
    monkeypatch.setattr(gate_mod.tailnet_name, "_name", None)
    monkeypatch.setattr(gate_mod.tailnet_name, "_path", str(HERE / "no-such-tailscale.exe"))
    assert send(app, "GET", "/api/status", OWNER)[0] == 403
    assert send(app, "GET", "/api/status", LOCAL)[0] == 200


def test_the_name_can_be_pinned():
    t = gate_mod.TailnetName("/nonexistent")
    t.pin("Epc.Tail-Test.ts.net.")
    assert t() == "epc.tail-test.ts.net"
