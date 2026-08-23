"""DJI DUML protocol — the binary framing the Osmo speaks over BLE.

A faithful Python port of lib-osmo-ble's protocol layer (yigitkonur/lib-osmo-ble),
which was itself built from Wireshark captures of the DJI Mimo app. Frame layout,
CRC parameters and command tables are theirs; the reference is in BLE.md.

Frame:
    0x55                      sync
    length (10 bits) + version (6 bits), 2 bytes LE
    CRC8 of the first 3 bytes
    target, 2 bytes LE        sender | (receiver << 8)
    sequence, 2 bytes BE      the one big-endian field in the frame
    flags, cmd_set, cmd_id    1 byte each
    payload                   variable
    CRC16 of everything above, 2 bytes LE

The CRCs are the fiddly part, so the engine below is verified against two
published standard check values (see tests/test_duml.py) before any DJI-specific
init constant is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

# ─── device addresses ───────────────────────────────────────────────────────
ADDR_CAMERA = 0x01
ADDR_APP = 0x02
ADDR_GIMBAL = 0x04
ADDR_WIFI = 0x07

# target = sender | (receiver << 8), written LE on the wire
TARGET_APP_TO_GIMBAL = ADDR_APP | (ADDR_GIMBAL << 8)  # 0x0402
TARGET_APP_TO_WIFI = ADDR_APP | (ADDR_WIFI << 8)      # 0x0702
TARGET_APP_TO_CAMERA = ADDR_APP | (ADDR_CAMERA << 8)  # 0x0102

# ─── flags ──────────────────────────────────────────────────────────────────
FLAG_REQUEST = 0x40
FLAG_RESPONSE = 0xC0
FLAG_NOTIFY = 0x00

# ─── command sets ───────────────────────────────────────────────────────────
CMD_SET_GENERAL = 0x00
CMD_SET_CAMERA = 0x01
CMD_SET_GIMBAL = 0x04
CMD_SET_BATTERY = 0x06
CMD_SET_WIFI = 0x07

# ─── gimbal commands (cmd_set 0x04) ─────────────────────────────────────────
GIMBAL_PARAMS_GET = 0x05   # position telemetry, pushed ~20 Hz
GIMBAL_SET_MODE = 0x4C     # lock / follow / fpv

GIMBAL_MODE_LOCK = 0
GIMBAL_MODE_FOLLOW = 1
GIMBAL_MODE_FPV = 2

# ─── wifi / pairing commands (cmd_set 0x07) ─────────────────────────────────
WIFI_SET_PAIRING_PIN = 0x45
WIFI_PAIRING_APPROVED = 0x46
WIFI_CONNECT = 0x47

# ─── defaults from lib-osmo-ble ─────────────────────────────────────────────
DEFAULT_PIN = "love"
DEFAULT_IDENTIFIER = "001749319286102"

# ─── BLE UUIDs ──────────────────────────────────────────────────────────────
BLE_SERVICE = "fff0"
BLE_CHAR_FFF4 = "fff4"  # pairing trigger + inbound notifications
BLE_CHAR_FFF5 = "fff5"  # command writes (write-without-response)


# ─── CRC ────────────────────────────────────────────────────────────────────

def _reflect(value: int, width: int) -> int:
    result = 0
    for i in range(width):
        if value & (1 << i):
            result |= 1 << (width - 1 - i)
    return result


def _crc_reflected(data: bytes, width: int, poly: int, init: int,
                   xorout: int) -> int:
    """Reflected (refin=refout=true) CRC, bit-by-bit.

    init is used directly in the reflected/LSB-first domain, matching the
    table-driven reflected form that lib-osmo-ble's `crc-full` uses. Verified
    against CRC-8/MAXIM and CRC-16/KERMIT in the tests, which share this exact
    model and differ from the DJI CRCs only in the init constant.
    """
    mask = (1 << width) - 1
    rpoly = _reflect(poly, width)
    reg = init & mask
    for byte in data:
        reg ^= byte
        for _ in range(8):
            if reg & 1:
                reg = (reg >> 1) ^ rpoly
            else:
                reg >>= 1
            reg &= mask
    return (reg ^ xorout) & mask


def crc8(data: bytes) -> int:
    return _crc_reflected(data, width=8, poly=0x31, init=0xEE, xorout=0x00)


def crc16(data: bytes) -> int:
    return _crc_reflected(data, width=16, poly=0x1021, init=0x496C, xorout=0x0000)


# ─── framing ────────────────────────────────────────────────────────────────

@dataclass
class DumlMessage:
    length: int
    sender: int
    receiver: int
    seq: int
    flags: int
    cmd_set: int
    cmd_id: int
    payload: bytes
    raw: bytes


class SequenceCounter:
    """DUML sequence starts at 0x0100 and increments per message."""

    def __init__(self, start: int = 0x0100) -> None:
        self.value = start

    def next(self) -> int:
        v = self.value
        self.value = (self.value + 1) & 0xFFFF
        return v


def pack_string(s: str) -> bytes:
    """One length byte, then the UTF-8 bytes -- DJI's string encoding."""
    raw = s.encode("utf-8")
    if len(raw) > 255:
        raise ValueError("string too long for a single length byte")
    return bytes([len(raw)]) + raw


def build_message(
    target: int,
    flags: int,
    cmd_set: int,
    cmd_id: int,
    payload: bytes = b"",
    seq: int = 0x0100,
    version: int = 1,
) -> bytes:
    total_len = 13 + len(payload)  # 11-byte header + payload + 2-byte CRC16
    if total_len > 0x3FF:
        raise ValueError("frame exceeds the 10-bit length field")

    head = bytearray()
    head.append(0x55)
    head.append(total_len & 0xFF)
    head.append(((total_len >> 8) & 0x03) | ((version & 0x3F) << 2))
    head.append(crc8(bytes(head)))

    body = bytearray(head)
    body += target.to_bytes(2, "little")
    body += seq.to_bytes(2, "big")
    body.append(flags)
    body.append(cmd_set)
    body.append(cmd_id)
    body += payload

    body += crc16(bytes(body)).to_bytes(2, "little")
    return bytes(body)


def parse_message(data: bytes) -> DumlMessage | None:
    if len(data) < 13 or data[0] != 0x55:
        return None
    length = data[1] | ((data[2] & 0x03) << 8)
    if len(data) < length:
        return None
    return DumlMessage(
        length=length,
        sender=data[4],
        receiver=data[5],
        seq=int.from_bytes(data[6:8], "big"),
        flags=data[8],
        cmd_set=data[9],
        cmd_id=data[10],
        payload=data[11:length - 2],
        raw=data[:length],
    )


def parse_stream(buffer: bytes) -> tuple[list[DumlMessage], bytes]:
    """Pull every complete frame out of a byte buffer; return the remainder.

    BLE delivers DUML in notification chunks that do not line up with frame
    boundaries, so a running buffer is resynchronised on the 0x55 sync byte.
    """
    messages: list[DumlMessage] = []
    buf = bytes(buffer)

    while len(buf) >= 13:
        idx = buf.find(0x55)
        if idx == -1:
            return messages, b""
        if idx > 0:
            buf = buf[idx:]
        if len(buf) < 4:
            break
        msg_len = buf[1] | ((buf[2] & 0x03) << 8)
        if msg_len < 13 or msg_len > 1024:
            buf = buf[1:]  # false sync byte; step past it
            continue
        if len(buf) < msg_len:
            break
        msg = parse_message(buf[:msg_len])
        buf = buf[msg_len:]
        if msg:
            messages.append(msg)
    return messages, buf


def verify(frame: bytes) -> bool:
    """True if both CRCs in a frame check out."""
    if len(frame) < 13 or frame[0] != 0x55:
        return False
    if crc8(frame[:3]) != frame[3]:
        return False
    length = frame[1] | ((frame[2] & 0x03) << 8)
    if len(frame) < length:
        return False
    body = frame[:length - 2]
    stored = int.from_bytes(frame[length - 2:length], "little")
    return crc16(body) == stored


# ─── payload builders ───────────────────────────────────────────────────────

def pairing_payload(identifier: str = DEFAULT_IDENTIFIER,
                    pin: str = DEFAULT_PIN) -> bytes:
    return pack_string(identifier) + pack_string(pin)


def gimbal_mode_payload(mode: int) -> bytes:
    return bytes([mode & 0xFF, 0x00])
