"""BLE control sidecar for the Osmo Pocket 3.

This is deliberately NOT a camera backend. Video comes over USB through the
osmo backend; this owns the separate Bluetooth radio and speaks DUML. The two
were measured to coexist -- USB webcam mode keeps streaming while a BLE session
is open (see BLE.md).

What is expected to work, from the reverse-engineering survey:

  * pairing (fff4 trigger + a SET_PAIRING_PIN command)
  * gimbal position telemetry, pushed ~20 Hz
  * battery level, if the camera pushes it

What is expected NOT to work over BLE alone: gimbal *motor* commands. Every
public implementation finds the Pocket 3 silently ignores them unless a Wi-Fi
streaming session is active; whether a USB webcam session also satisfies that
is one of the open questions this module exists to answer. So set_mode() is
provided but treated as unproven until a run confirms it.

Everything is best-effort and reports what actually happened rather than
asserting success -- the same rule as the rest of camrig: never claim a
capability the hardware has not demonstrated.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import duml

log = logging.getLogger(__name__)

try:
    from bleak import BleakClient, BleakScanner
    _HAVE_BLEAK = True
except ImportError:  # keep the rest of camrig importable without bleak
    _HAVE_BLEAK = False


# Full 128-bit forms; bleak matches on these.
def _uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


SERVICE = _uuid(duml.BLE_SERVICE)
FFF4 = _uuid(duml.BLE_CHAR_FFF4)
FFF5 = _uuid(duml.BLE_CHAR_FFF5)

OSMO_HINTS = ("osmo", "dji", "pocket")
DJI_COMPANY_ID = 0x08AA  # seen in the Pocket 3 advertisement (BLE.md)


@dataclass
class GimbalState:
    pitch: float = 0.0
    roll: float = 0.0
    yaw: float = 0.0
    mode: int | None = None
    updated_at: float = 0.0


@dataclass
class BleState:
    connected: bool = False
    paired: bool = False
    address: str | None = None
    name: str | None = None
    battery: int | None = None
    gimbal: GimbalState = field(default_factory=GimbalState)
    last_error: str | None = None
    messages_in: int = 0

    def as_dict(self) -> dict[str, Any]:
        g = self.gimbal
        age = round(time.monotonic() - g.updated_at, 2) if g.updated_at else None
        return {
            "connected": self.connected,
            "paired": self.paired,
            "address": self.address,
            "name": self.name,
            "battery": self.battery,
            "gimbal": {
                "pitch": g.pitch, "roll": g.roll, "yaw": g.yaw,
                "mode": g.mode, "telemetry_age": age,
            },
            "messages_in": self.messages_in,
            "last_error": self.last_error,
        }


async def find_osmo(timeout: float = 12.0):
    """Return (address, name) for the first Osmo seen, or (None, None)."""
    if not _HAVE_BLEAK:
        raise RuntimeError("bleak is not installed")

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    best = None
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        is_osmo = (
            any(h in name.lower() for h in OSMO_HINTS)
            or DJI_COMPANY_ID in adv.manufacturer_data
        )
        if is_osmo:
            rssi = adv.rssi if adv.rssi is not None else -200
            if best is None or rssi > best[2]:
                best = (device.address, name, rssi)
    if best:
        return best[0], best[1]
    return None, None


class OsmoBle:
    """An async BLE session. Own its lifecycle with `async with`, or call
    connect()/disconnect() directly."""

    def __init__(self, address: str | None = None,
                 identifier: str = duml.DEFAULT_IDENTIFIER,
                 pin: str = duml.DEFAULT_PIN) -> None:
        if not _HAVE_BLEAK:
            raise RuntimeError("bleak is not installed")
        self.address = address
        self.identifier = identifier
        self.pin = pin
        self.state = BleState(address=address)
        self._client: "BleakClient | None" = None
        self._seq = duml.SequenceCounter()
        self._rx = bytearray()
        self._paired_event = asyncio.Event()

    # -- lifecycle ------------------------------------------------------

    async def connect(self, timeout: float = 20.0) -> None:
        if self.address is None:
            addr, name = await find_osmo()
            if addr is None:
                raise RuntimeError("no Osmo found in a BLE scan")
            self.address = addr
            self.state.address = addr
            self.state.name = name

        client = BleakClient(self.address, timeout=timeout)
        await client.connect()
        self._client = client
        self.state.connected = True

        await client.start_notify(FFF5, self._on_notify)
        await client.start_notify(FFF4, self._on_notify)
        log.info("BLE connected to %s", self.address)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self.state.connected = False
        self.state.paired = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.debug("disconnect: %s", exc)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
        return False

    # -- pairing --------------------------------------------------------

    async def pair(self, timeout: float = 15.0) -> bool:
        """Trigger pairing and send credentials. Returns True if approved.

        The camera may show a confirmation prompt on its own screen the first
        time; approving there is what flips PAIRING_APPROVED.
        """
        if self._client is None:
            raise RuntimeError("not connected")

        self._paired_event.clear()
        # Trigger: written WITH response to fff4.
        await self._client.write_gatt_char(FFF4, bytes([0x01, 0x00]), response=True)
        await asyncio.sleep(0.2)

        frame = duml.build_message(
            duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
            duml.CMD_SET_WIFI, duml.WIFI_SET_PAIRING_PIN,
            duml.pairing_payload(self.identifier, self.pin),
            seq=self._seq.next(),
        )
        await self._write_cmd(frame)

        try:
            await asyncio.wait_for(self._paired_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("pairing timed out after %.0fs", timeout)
        return self.state.paired

    # -- commands -------------------------------------------------------

    async def set_gimbal_mode(self, mode: int) -> None:
        """lock=0 / follow=1 / fpv=2. UNPROVEN over BLE -- see module docstring."""
        frame = duml.build_message(
            duml.TARGET_APP_TO_GIMBAL, duml.FLAG_REQUEST,
            duml.CMD_SET_GIMBAL, duml.GIMBAL_SET_MODE,
            duml.gimbal_mode_payload(mode), seq=self._seq.next(),
        )
        await self._write_cmd(frame)

    async def _write_cmd(self, frame: bytes) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        # write-without-response: the only characteristic that processes DUML.
        await self._client.write_gatt_char(FFF5, frame, response=False)

    # -- inbound --------------------------------------------------------

    def _on_notify(self, _char, data: bytearray) -> None:
        self._rx += data
        messages, remaining = duml.parse_stream(bytes(self._rx))
        self._rx = bytearray(remaining)
        for msg in messages:
            self.state.messages_in += 1
            self._route(msg)

    def _route(self, msg: duml.DumlMessage) -> None:
        if msg.cmd_set == duml.CMD_SET_GIMBAL:
            if msg.cmd_id == duml.GIMBAL_PARAMS_GET and len(msg.payload) >= 6:
                g = self.state.gimbal
                g.pitch = int.from_bytes(msg.payload[0:2], "little", signed=True) / 10
                g.roll = int.from_bytes(msg.payload[2:4], "little", signed=True) / 10
                g.yaw = int.from_bytes(msg.payload[4:6], "little", signed=True) / 10
                if len(msg.payload) >= 7:
                    g.mode = msg.payload[6]
                g.updated_at = time.monotonic()
            return

        if msg.cmd_set == duml.CMD_SET_WIFI:
            if msg.cmd_id == duml.WIFI_SET_PAIRING_PIN and (msg.flags & 0x80):
                status = msg.payload[1] if len(msg.payload) >= 2 else (
                    msg.payload[0] if msg.payload else 0)
                if status == 0x01:  # already paired
                    self.state.paired = True
                    self._paired_event.set()
            elif msg.cmd_id == duml.WIFI_PAIRING_APPROVED and msg.payload[:1] == b"\x01":
                self.state.paired = True
                self._paired_event.set()
            return

        if msg.cmd_set == duml.CMD_SET_BATTERY and msg.payload:
            self.state.battery = msg.payload[0]
            return
