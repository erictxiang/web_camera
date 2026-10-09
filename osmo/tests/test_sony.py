"""Sony backend and the settings/root-path surface, all without hardware.

The gphoto2 bindings are stood in for by a small in-memory double that
behaves the way the a6000 was measured to on 2026-09-04: enumerated choices,
exposure widgets read-only unless the dial is on M, ISO auto spelled
"Auto ISO", capture returning a JPEG in a capt_ pseudo folder. Anything the
real body does that the double does not is a gap to fill from a new probe,
not something to guess at here.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camrig import CameraError, FakeBackend  # noqa: E402
from camrig.sony import SETTINGS, SonyBackend, jpeg_dimensions, match_choice  # noqa: E402
from server import create_app, normalize_root_path  # noqa: E402


# -- a gphoto2 stand-in -------------------------------------------------


def tiny_jpeg(width: int, height: int) -> bytes:
    """SOI, one APP0 segment, one SOF0 with the given size, EOI. Enough for
    the dimension parser; not decodable, and does not need to be."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


class GPhoto2Error(Exception):
    pass


class Widget:
    def __init__(self, label, value, choices, readonly=False):
        self.label, self.value, self.choices, self.readonly = label, value, list(choices), readonly

    def get_label(self):
        return self.label

    def get_value(self):
        return self.value

    def set_value(self, v):
        self.value = v

    def get_choices(self):
        return list(self.choices)

    def get_readonly(self):
        return 1 if self.readonly else 0


class Config:
    def __init__(self, widgets):
        self.widgets = widgets

    def get_child_by_name(self, name):
        try:
            return self.widgets[name]
        except KeyError:
            raise GPhoto2Error(f"{name} not found")


class Abilities:
    def __init__(self, model):
        self.model = model


class FilePath:
    def __init__(self, folder, name):
        self.folder, self.name = folder, name


class CameraFile:
    def __init__(self, data):
        self._data = data

    def get_data_and_size(self):
        return self._data


class FakeCamera:
    """One a6000, with the dial and the sleep state as mutable knobs."""

    def __init__(self, gp):
        self.gp = gp
        self.inited = False
        self.exited = False
        self.deleted: list[str] = []
        self.captures = 0
        self.set_calls = 0

    def init(self):
        if self.gp.asleep:
            raise GPhoto2Error("[-105] Unknown model")
        self.inited = True

    def exit(self):
        self.exited = True

    def get_abilities(self):
        return Abilities(self.gp.model)

    def _check(self):
        if self.gp.asleep:
            raise GPhoto2Error("[-53] Could not claim the USB device")

    def get_config(self):
        self._check()
        return Config(self.gp.widgets)

    def set_config(self, cfg):
        self._check()
        self.set_calls += 1

    def capture(self, kind):
        self._check()
        if self.gp.capture_error:
            raise GPhoto2Error(self.gp.capture_error)
        self.captures += 1
        return FilePath("/", f"capt_DSC{self.captures:05d}.{self.gp.ext}")

    def file_get(self, folder, name, kind):
        self._check()
        return CameraFile(self.gp.image)

    def file_delete(self, folder, name):
        self._check()
        if self.gp.delete_error:
            raise GPhoto2Error(self.gp.delete_error)
        self.deleted.append(name)

    def wait_for_event(self, timeout_ms):
        self._check()
        self.gp.polls += 1
        return self.gp.GP_EVENT_TIMEOUT, None


class FakeGP:
    GP_EVENT_TIMEOUT = 1
    GP_CAPTURE_IMAGE = 0
    GP_FILE_TYPE_NORMAL = 1
    GPhoto2Error = GPhoto2Error

    def __init__(self, dial="M"):
        self.model = "Sony Alpha-A6000 (Control)"
        self.asleep = False
        self.polls = 0
        self.ext = "JPG"
        self.image = tiny_jpeg(6000, 4000)
        self.capture_error = None
        self.delete_error = None
        self.applied = {}
        self.cameras: list[FakeCamera] = []
        locked = dial != "M"
        self.widgets = {
            "expprogram": Widget("Exposure Program", dial, ["M", "P", "Intelligent Auto"], True),
            "shutterspeed": Widget("Shutter Speed", "1/125", ["1/500", "1/250", "1/125", "1/60"], locked),
            "f-number": Widget("F-Number", "f/3.5", ["f/3.5", "f/4", "f/5.6", "f/8"], locked),
            "iso": Widget("ISO Speed", "Auto ISO", ["Auto ISO", "100", "200", "400"], locked),
            "exposurecompensation": Widget("Exposure Compensation", "0", ["-1", "0", "1"], locked),
            "whitebalance": Widget("WhiteBalance", "Automatic", ["Automatic", "Tungsten"], False),
            "focusmode": Widget("Focus Mode", "Automatic", ["Automatic", "Manual"], False),
            "imagequality": Widget("Image Quality", "Fine", ["Fine", "RAW"], False),
            "capturemode": Widget("Still Capture Mode", "Single Shot", ["Single Shot"], False),
            "batterylevel": Widget("Battery Level", "75%", [], True),
            "usbmode": Widget("USB Mode", "PC Remote", ["PC Remote", "MTP"], False),
        }

    def Camera(self):
        cam = FakeCamera(self)
        self.cameras.append(cam)
        return cam


@pytest.fixture
def gp():
    return FakeGP()


@pytest.fixture
def sony(gp):
    b = SonyBackend(gp=gp)
    b.keepalive_interval = 0.05
    b.stale_after = 0.5
    b.settle = 0.5
    b.open()
    yield b
    b.close()


# -- helpers ------------------------------------------------------------


def test_jpeg_dimensions_reads_sof():
    assert jpeg_dimensions(tiny_jpeg(6000, 4000)) == (6000, 4000)
    assert jpeg_dimensions(b"not a jpeg") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("f/5.6", "f/5.6"),
        ("5.6", "f/5.6"),
        ("F/5.6", "f/5.6"),
        ("Auto ISO", "Auto ISO"),
        ("auto iso", "Auto ISO"),
        ("Auto", None),  # the real token is "Auto ISO"; never guess
        ("1/1000", None),
    ],
)
def test_match_choice(value, expected):
    choices = ["f/3.5", "f/5.6", "Auto ISO", "1/125"]
    assert match_choice(value, choices) == expected


def test_normalize_root_path():
    assert normalize_root_path("") == ""
    assert normalize_root_path("/") == ""
    assert normalize_root_path("sony") == "/sony"
    assert normalize_root_path("/sony/") == "/sony"


# -- backend ------------------------------------------------------------


def test_capabilities_are_honest(sony):
    caps = sony.capabilities
    assert caps.native_stills and caps.still_capture and caps.settings
    assert not caps.live_view and not caps.video_record
    assert caps.max_still == (6000, 4000)
    assert sony.read_frame() is None


def test_open_refuses_a_body_not_in_pc_remote(gp):
    gp.model = "Sony Alpha-A6000 (MTP)"
    b = SonyBackend(gp=gp)
    with pytest.raises(CameraError) as exc:
        b.open()
    assert "PC Remote" in str(exc.value)
    assert gp.cameras[-1].exited, "a rejected session must be released"


def test_open_explains_a_missing_camera(gp):
    gp.asleep = True
    b = SonyBackend(gp=gp)
    with pytest.raises(CameraError) as exc:
        b.open()
    assert "usbipd" in str(exc.value)


def test_capture_downloads_deletes_and_measures(sony, tmp_path, gp):
    result = sony.capture_still(tmp_path / "frame_00000.jpg")
    assert result.ok, result.error
    assert (result.width, result.height) == (6000, 4000)
    assert result.bytes == len(gp.image)
    assert Path(result.path).read_bytes() == gp.image
    assert result.meta["deleted_on_camera"] is True
    assert gp.cameras[-1].deleted == ["capt_DSC00001.JPG"]
    assert "captures" in sony.status()["details"]
    assert sony.status()["details"]["captures"] == 1


def test_raw_capture_keeps_its_extension(sony, tmp_path, gp):
    gp.ext = "ARW"
    gp.image = b"\x00" * 64
    result = sony.capture_still(tmp_path / "frame_00000.jpg")
    assert result.ok
    assert result.path.endswith(".arw")
    assert result.width is None  # not a JPEG, not parsed


def test_capture_failure_returns_rather_than_raises(sony, tmp_path, gp):
    gp.capture_error = "[-110] I/O in progress"
    result = sony.capture_still(tmp_path / "x.jpg")
    assert result.ok is False
    assert "I/O" in result.error


def test_failed_delete_is_flagged_not_fatal(sony, tmp_path, gp):
    gp.delete_error = "[-6] Unsupported operation"
    result = sony.capture_still(tmp_path / "x.jpg")
    assert result.ok
    assert result.meta["deleted_on_camera"] is False


def test_settings_are_the_allowlist_only(sony):
    settings = sony.get_settings()
    assert tuple(settings) == SETTINGS
    assert "usbmode" not in settings, "nothing outside the allowlist may leak out"
    assert settings["iso"]["choices"][0] == "Auto ISO"
    assert settings["expprogram"]["readonly"] is True


def test_set_setting_returns_the_camera_token(sony, gp):
    assert sony.set_setting("f-number", "5.6") == "f/5.6"
    assert gp.widgets["f-number"].value == "f/5.6"
    assert sony.set_setting("iso", "400") == "400"
    assert sony.set_setting("iso", "auto iso") == "Auto ISO"
    with pytest.raises(CameraError) as exc:
        sony.set_setting("iso", "Auto")
    assert "Auto ISO" in str(exc.value)


def test_set_setting_rejects_outside_the_allowlist(sony):
    with pytest.raises(CameraError):
        sony.set_setting("usbmode", "MTP")


def test_exposure_is_locked_off_the_m_dial():
    gp = FakeGP(dial="Intelligent Auto")
    b = SonyBackend(gp=gp)
    b.keepalive_interval = 0.05
    b.open()
    try:
        with pytest.raises(CameraError) as exc:
            b.set_setting("shutterspeed", "1/250")
        assert "Intelligent Auto" in str(exc.value)
        st = b.status()
        assert st["details"]["mode dial"] == "Intelligent Auto"
        assert "warning" in st
        # Non-exposure widgets still work regardless of the dial.
        assert b.set_setting("focusmode", "Manual") == "Manual"
    finally:
        b.close()


def test_set_setting_fails_when_the_body_does_not_take_it(sony, gp):
    class Stubborn(Widget):
        def set_value(self, v):
            pass  # the body ignores the write

    gp.widgets["shutterspeed"] = Stubborn("Shutter Speed", "1/125", ["1/250", "1/125"])
    with pytest.raises(CameraError) as exc:
        sony.set_setting("shutterspeed", "1/250")
    assert "did not take" in str(exc.value)


def test_keepalive_polls_and_reports_health(sony, gp):
    time.sleep(0.3)
    assert gp.polls >= 2
    assert sony.healthy
    st = sony.status()
    assert st["link_age"] is not None and st["link_age"] < 0.5
    assert st["details"]["model"] == "Sony Alpha-A6000 (Control)"
    assert st["details"]["battery"] == "75%"


def test_sleep_is_detected_and_reconnect_is_automatic(sony, gp):
    """The body sleeping is the failure mode seen on the bench: every call
    fails until it is woken, then a fresh init() succeeds."""
    gp.asleep = True
    deadline = time.monotonic() + 3
    while sony.healthy and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not sony.healthy
    assert sony.status()["keepalive_failures"] >= 1
    assert not sony.capture_still(Path("unused.jpg")).ok

    gp.asleep = False
    deadline = time.monotonic() + 3
    while not sony.healthy and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sony.healthy, "keepalive did not reconnect after the body woke"
    assert sony.status()["details"]["reconnects"] == 1
    assert len(gp.cameras) >= 2


def test_close_exits_the_session_and_is_idempotent(gp):
    b = SonyBackend(gp=gp)
    b.keepalive_interval = 0.05
    b.open()
    b.close()
    b.close()
    assert gp.cameras[-1].exited
    assert not b.opened


def test_calls_are_serialized_under_one_lock(sony, gp):
    """libgphoto2 is not thread-safe: two callers must never be inside it at
    once, even with the keepalive running alongside."""
    inside = 0
    overlap = []
    real = FakeCamera.get_config

    def guarded(self):
        nonlocal inside
        inside += 1
        if inside > 1:
            overlap.append(inside)
        time.sleep(0.01)
        inside -= 1
        return real(self)

    FakeCamera.get_config = guarded
    try:
        threads = [threading.Thread(target=sony.get_settings) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        FakeCamera.get_config = real
    assert not overlap, f"concurrent libgphoto2 calls observed: {overlap}"


# -- API: settings and root path ------------------------------------------


@pytest.fixture
def client(tmp_path):
    b = FakeBackend(width=320, height=240, fps=60, settings=True)
    app = create_app(b, tmp_path / "captures", stream_fps=30, root_path="/sony")
    with TestClient(app) as c:
        c.backend = b
        yield c


def test_settings_endpoints(client):
    body = client.get("/api/settings").json()["settings"]
    assert body["shutterspeed"]["value"] == "1/125"

    r = client.post("/api/settings", json={"key": "shutterspeed", "value": "1/250"})
    assert r.status_code == 200 and r.json()["value"] == "1/250"
    assert client.get("/api/settings").json()["settings"]["shutterspeed"]["value"] == "1/250"

    # Bad value, unknown key and a read-only widget are all 400 with a reason.
    for payload in (
        {"key": "shutterspeed", "value": "1/7"},
        {"key": "usbmode", "value": "MTP"},
        {"key": "expprogram", "value": "P"},
    ):
        r = client.post("/api/settings", json=payload)
        assert r.status_code == 400, payload
        assert r.json()["detail"]

    assert client.post("/api/settings", json={"key": "", "value": "x"}).status_code == 422


def test_settings_are_400_on_a_backend_without_them(tmp_path):
    app = create_app(FakeBackend(width=160, height=120, fps=60), tmp_path / "c")
    with TestClient(app) as c:
        assert c.get("/api/settings").status_code == 400
        assert c.post("/api/settings", json={"key": "iso", "value": "100"}).status_code == 400


def test_root_path_routes_with_and_without_the_prefix(client):
    """Whether or not the proxy strips /sony, the same app answers."""
    assert client.get("/sony/api/status").status_code == 200
    assert client.get("/api/status").status_code == 200


def test_root_path_is_baked_into_the_page_and_every_url(client):
    page = client.get("/sony/").text
    assert '<base href="/sony/">' in page
    assert 'api("/api/' not in page, "the page must fetch relative to <base>"
    assert "`/api/stream" not in page

    snap = client.post("/sony/api/snapshot").json()
    assert snap["url"].startswith("/sony/captures/")
    assert client.get(snap["url"]).status_code == 200

    listing = client.get("/sony/api/captures").json()["captures"]
    assert listing and all(c["url"].startswith("/sony/captures/") for c in listing)

    assert "/sony/openapi.json" in client.get("/sony/docs").text


def test_no_root_path_keeps_the_old_urls(tmp_path):
    app = create_app(FakeBackend(width=160, height=120, fps=60), tmp_path / "c")
    with TestClient(app) as c:
        assert '<base href="/">' in c.get("/").text
        assert c.post("/api/snapshot").json()["url"].startswith("/captures/")
