"""Regression tests, all against the fake backend -- no hardware, CI-safe.

These target the things manual testing never reaches: the concurrency design,
the failure paths, and the guards. Everything the handoff listed as unverified
is here.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camrig import CameraError, FakeBackend, TimelapseRunner  # noqa: E402
from camrig.timelapse import TimelapseError  # noqa: E402
from server import create_app, safe_capture_path  # noqa: E402


@pytest.fixture
def backend():
    b = FakeBackend(width=320, height=240, fps=60)
    b.open()
    yield b
    b.close()


@pytest.fixture
def client(tmp_path):
    b = FakeBackend(width=320, height=240, fps=60)
    app = create_app(b, tmp_path / "captures", stream_fps=30)
    with TestClient(app) as c:
        c.backend = b
        c.captures = tmp_path / "captures"
        yield c


# -- the buffer contract ------------------------------------------------


def test_read_frame_returns_a_private_copy(backend):
    """Consumers must never receive the buffer itself."""
    a = backend.read_frame()
    b = backend.read_frame()
    assert a is not None and b is not None
    assert a is not b

    a[:] = 0
    with backend._frame_lock:
        assert backend._frame is not None
        assert backend._frame.any(), "mutating a returned frame corrupted the buffer"


def test_frames_are_internally_consistent_under_concurrent_readers(backend):
    """A torn frame would mix two generations inside one buffer.

    Each fake frame carries a solid single-valued marker block; more than one
    distinct value in it means a reader saw two grabs at once.
    """
    errors: list[str] = []

    def reader():
        for _ in range(200):
            frame = backend.read_frame()
            if frame is None:
                continue
            values = backend.marker_values(frame)
            if values.size != 1:
                errors.append(f"torn frame: {values.size} generations present")

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:3]


def test_open_close_cycles_do_not_crash():
    """Eight cycles in a row -- the shutdown path is where the segfault lived."""
    b = FakeBackend(width=160, height=120, fps=60)
    for _ in range(8):
        b.open()
        assert b.read_frame() is not None
        b.close()
    assert not b.opened


def test_close_is_idempotent(backend):
    backend.close()
    backend.close()


class BlockingFake(FakeBackend):
    """A fake whose grab can be made to block longer than the join timeout.

    That is the unplug case: cap.read() sits in a driver call that never
    returns, so close() cannot join the thread and must leak the handle.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.block = False
        self.blocked = threading.Event()
        self.grab_threads: list[int] = []

    def _grab(self, source):
        self.grab_threads.append(threading.get_ident())
        if self.block:
            # Announce that we are inside the uninterruptible read, so the
            # test can close() at the one moment that forces a leak rather
            # than racing the thread and sometimes missing it.
            self.blocked.set()
            time.sleep(0.6)
            return None
        return super()._grab(source)


def test_reopen_after_a_leaked_handle_leaves_one_writer(tmp_path):
    """Replug recovery must not leave a zombie thread on the new device.

    If a leaked grab thread survives into the next session it will write into
    the buffer of a device it does not own -- two readers on one capture, the
    exact failure the one-slot design exists to prevent.
    """
    b = BlockingFake(width=160, height=120, fps=60)
    b.join_timeout = 0.2
    b.open()
    assert b.read_frame() is not None

    b.block = True
    assert b.blocked.wait(2.0), "grab thread never entered the blocking read"
    b.close()
    assert b._leaked, "expected the blocked thread to force a leak"

    b.block = False
    b.grab_threads.clear()
    b.open()
    try:
        time.sleep(0.5)
        writers = set(b.grab_threads)
        assert len(writers) == 1, (
            f"{len(writers)} threads are grabbing after reopen; "
            f"a leaked thread survived into the new session"
        )
    finally:
        b.close()


def test_reopen_requires_a_genuinely_new_frame():
    """First light must be checked per session, not against a lifetime total.

    Otherwise the second open() sees a frame counter left over from the first
    and reports success against a device that is delivering nothing.
    """
    b = FakeBackend(width=160, height=120, fps=60)
    b.open()
    assert b.read_frame() is not None
    b.close()

    b.stall_after = -1  # produce nothing at all from here on
    b.stale_after = 0.5
    with pytest.raises(CameraError):
        b.open()
    b.close()


def test_open_failure_raises_and_is_recorded():
    b = FakeBackend(fail_open=True)
    with pytest.raises(CameraError):
        b.open()
    assert not b.opened
    assert b.last_error is not None


def test_stalled_source_goes_unhealthy_without_hanging():
    """The watchdog substitutes for CAP_PROP_READ_TIMEOUT_MSEC, which DSHOW
    rejects. It cannot unblock a read, but health must still tell the truth."""
    b = FakeBackend(width=160, height=120, fps=60, stall_after=3)
    b.stale_after = 0.4
    b.open()
    try:
        assert b.healthy
        time.sleep(0.7)
        assert not b.healthy
        # A stale buffer still serves its last frame; it does not blow up.
        assert b.read_frame() is not None
    finally:
        b.close()


# -- timelapse ----------------------------------------------------------


def test_interval_accuracy_is_absolute_not_cumulative(backend, tmp_path):
    runner = TimelapseRunner(backend, tmp_path)
    interval, count = 0.1, 8
    started = time.monotonic()
    runner.start(interval=interval, count=count)

    deadline = time.monotonic() + 10
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.02)
    elapsed = time.monotonic() - started

    state = runner.state()
    assert state["captured"] == count, state
    assert not state["running"]

    # Absolute scheduling: total time tracks (count-1)*interval regardless of
    # how long each capture took. A sleep(interval) loop would overshoot by the
    # accumulated capture time.
    expected = (count - 1) * interval
    assert abs(elapsed - expected) < 0.25, f"drifted: {elapsed:.3f}s vs {expected:.3f}s"

    written = sorted(Path(state["directory"]).glob("frame_*.jpg"))
    assert len(written) == count
    assert all(p.stat().st_size > 0 for p in written)


def test_interval_accuracy_holds_under_load(backend, tmp_path):
    """Drift must not depend on the machine being idle."""
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            backend.read_frame()

    load = [threading.Thread(target=churn, daemon=True) for _ in range(4)]
    for t in load:
        t.start()

    try:
        runner = TimelapseRunner(backend, tmp_path)
        started = time.monotonic()
        runner.start(interval=0.1, count=6)
        deadline = time.monotonic() + 10
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.02)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        for t in load:
            t.join(timeout=1)

    assert runner.state()["captured"] == 6
    assert abs(elapsed - 0.5) < 0.3, f"drifted under load: {elapsed:.3f}s"


def test_capture_failure_is_counted_and_the_run_continues(tmp_path):
    """One bad frame must never end a run."""
    b = FakeBackend(width=160, height=120, fps=60, fail_every=3)
    b.open()
    try:
        runner = TimelapseRunner(b, tmp_path)
        runner.start(interval=0.05, count=9)
        deadline = time.monotonic() + 10
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.02)

        state = runner.state()
        assert state["failed"] == 3, state
        assert state["captured"] == 6, state
        assert state["captured"] + state["failed"] == 9
        assert state["last_error"]
    finally:
        b.close()


def test_capture_failure_returns_a_result_rather_than_raising(tmp_path):
    b = FakeBackend(width=160, height=120, fps=60, fail_every=1)
    b.open()
    try:
        result = b.capture_still(tmp_path / "x.jpg")
        assert result.ok is False
        assert result.error
    finally:
        b.close()


def test_double_start_is_rejected(backend, tmp_path):
    runner = TimelapseRunner(backend, tmp_path)
    runner.start(interval=0.2, count=20)
    try:
        with pytest.raises(TimelapseError):
            runner.start(interval=0.2, count=20)
    finally:
        runner.stop()


def test_stop_halts_an_unbounded_run(backend, tmp_path):
    runner = TimelapseRunner(backend, tmp_path)
    runner.start(interval=0.05, count=None)
    time.sleep(0.3)
    state = runner.stop()
    assert not runner.running
    assert state["captured"] >= 2


# -- API ----------------------------------------------------------------


def test_status_reports_capabilities(client):
    body = client.get("/api/status").json()
    caps = body["camera"]["capabilities"]
    assert caps["live_view"] and caps["still_capture"]
    # A backend must not claim what it cannot honour.
    assert caps["native_stills"] is False
    assert caps["video_record"] is False
    assert body["camera"]["healthy"] is True


def test_backend_open_failure_surfaces_in_status(tmp_path):
    """The server must stay up and explain itself, not crash on boot."""
    b = FakeBackend(fail_open=True)
    app = create_app(b, tmp_path / "captures")
    with TestClient(app) as c:
        body = c.get("/api/status").json()
        assert body["camera"]["open_error"]
        assert body["camera"]["healthy"] is False
        assert c.post("/api/snapshot").status_code == 503


def test_snapshot_writes_a_real_jpeg(client):
    body = client.post("/api/snapshot").json()
    assert body["ok"] and body["width"] == 320 and body["height"] == 240
    written = client.captures / body["name"]
    assert written.is_file()
    assert written.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"


def test_stream_serves_decodable_jpegs(client):
    import cv2

    with client.stream("GET", "/api/stream?frames=3") as res:
        assert res.status_code == 200
        assert "multipart/x-mixed-replace" in res.headers["content-type"]
        buf = b"".join(res.iter_bytes())
    assert buf.count(b"--camrigframe\r\n") == 3

    # Pull one part out of the multipart body and prove it decodes.
    start = buf.index(b"\xff\xd8")
    end = buf.index(b"\xff\xd9", start) + 2
    frame = cv2.imdecode(np.frombuffer(buf[start:end], np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None
    assert frame.shape == (240, 320, 3)


def test_stream_frames_must_be_positive(client):
    assert client.get("/api/stream?frames=0").status_code == 422


def test_single_frame_endpoint(client):
    res = client.get("/api/frame.jpg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content[:2] == b"\xff\xd8"


def test_stream_and_timelapse_run_concurrently(client):
    """The reason the one-slot buffer exists."""
    started = client.post(
        "/api/timelapse/start", json={"interval": 0.05, "count": 8}
    )
    assert started.status_code == 200

    with client.stream("GET", "/api/stream?frames=4") as res:
        buf = b"".join(res.iter_bytes())
    assert buf.count(b"--camrigframe\r\n") == 4

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get("/api/timelapse").json()
        if not state["running"]:
            break
        time.sleep(0.05)

    state = client.get("/api/timelapse").json()
    assert state["captured"] == 8, state
    assert state["failed"] == 0


def test_double_start_returns_409(client):
    assert client.post("/api/timelapse/start", json={"interval": 1, "count": 50}).status_code == 200
    assert client.post("/api/timelapse/start", json={"interval": 1, "count": 50}).status_code == 409
    client.post("/api/timelapse/stop")


@pytest.mark.parametrize(
    "payload",
    [
        {"interval": 0, "count": 5},
        {"interval": -1, "count": 5},
        {"interval": 1, "count": 0},
        {"interval": 1, "count": -3},
        {"interval": "soon", "count": 5},
        {"count": 5},
    ],
)
def test_invalid_timelapse_parameters_return_422(client, payload):
    assert client.post("/api/timelapse/start", json=payload).status_code == 422


# -- the traversal guard ------------------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "../server.py",
        "../../etc/passwd",
        "....//....//server.py",
        "sub/../../server.py",
    ],
)
def test_capture_traversal_is_404(client, attack):
    res = client.get(f"/captures/{attack}")
    assert res.status_code == 404, f"{attack} leaked: {res.status_code}"


def test_safe_capture_path_rejects_escapes(tmp_path):
    root = tmp_path / "captures"
    root.mkdir()
    (root / "ok.jpg").write_bytes(b"x")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")

    from fastapi import HTTPException

    assert safe_capture_path(root, "ok.jpg").name == "ok.jpg"
    for bad in ("../secret.txt", "..", str(outside), "missing.jpg"):
        with pytest.raises(HTTPException) as exc:
            safe_capture_path(root, bad)
        assert exc.value.status_code == 404


def test_capture_listing_includes_timelapse_subfolders(client, tmp_path):
    client.post("/api/timelapse/start", json={"interval": 0.05, "count": 3})
    deadline = time.monotonic() + 10
    while client.get("/api/timelapse").json()["running"] and time.monotonic() < deadline:
        time.sleep(0.05)

    listing = client.get("/api/captures").json()["captures"]
    assert len(listing) >= 3
    for item in listing:
        assert client.get(item["url"]).status_code == 200
