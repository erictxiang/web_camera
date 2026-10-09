"""Test setup shared by every test file (home_server S3-22, #154).

The server is owner-gated (camrig/gate.py), so a test that drives it through
Starlette's TestClient must look like the owner's own PC: HOME_OWNER set, a loopback
Host, and the Content-Type the camera page's fetch() sends on every write. The patch
below gives TestClient those defaults; a test that wants the gate's refusals sends its
own headers (tests/test_gate.py builds raw ASGI requests and uses none of this).
"""
from __future__ import annotations

import os

os.environ.setdefault("HOME_OWNER", "owner@example.com")

from starlette.testclient import TestClient  # noqa: E402

_init = TestClient.__init__


def _loopback_init(self, app, base_url="http://127.0.0.1", *args, headers=None, **kwargs):
    headers = {"Content-Type": "application/json", **(headers or {})}
    _init(self, app, base_url, *args, headers=headers, **kwargs)


TestClient.__init__ = _loopback_init
