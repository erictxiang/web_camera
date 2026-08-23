"""DUML protocol tests -- pure, no BLE, no hardware.

The CRC engine is the part most likely to be subtly wrong, so it is pinned to
two PUBLISHED standard check values before any DJI-specific constant is
trusted. Both share the exact reflected model of the DJI CRCs and differ only
in the init constant, so reproducing them proves the reflection and polynomial
machinery is correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camrig import duml  # noqa: E402

CHECK = b"123456789"


def test_crc_engine_matches_crc8_maxim():
    # CRC-8/MAXIM: poly 0x31, init 0x00, refin/refout true, xorout 0 -> 0xA1
    got = duml._crc_reflected(CHECK, 8, 0x31, 0x00, 0x00)
    assert got == 0xA1, f"{got:#04x}"


def test_crc_engine_matches_crc16_kermit():
    # CRC-16/KERMIT: poly 0x1021, init 0x0000, refin/refout true, xorout 0 -> 0x2189
    got = duml._crc_reflected(CHECK, 16, 0x1021, 0x0000, 0x0000)
    assert got == 0x2189, f"{got:#06x}"


def test_reflect():
    assert duml._reflect(0x01, 8) == 0x80
    assert duml._reflect(0x80, 8) == 0x01
    assert duml._reflect(0x1021, 16) == 0x8408


def test_build_then_parse_roundtrips():
    frame = duml.build_message(
        duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
        duml.CMD_SET_WIFI, duml.WIFI_SET_PAIRING_PIN,
        duml.pairing_payload(), seq=0x0100,
    )
    msg = duml.parse_message(frame)
    assert msg is not None
    assert msg.sender == duml.ADDR_APP
    assert msg.receiver == duml.ADDR_WIFI
    assert msg.seq == 0x0100
    assert msg.flags == duml.FLAG_REQUEST
    assert msg.cmd_set == duml.CMD_SET_WIFI
    assert msg.cmd_id == duml.WIFI_SET_PAIRING_PIN
    assert msg.payload == duml.pairing_payload()


def test_built_frame_self_verifies():
    frame = duml.build_message(
        duml.TARGET_APP_TO_GIMBAL, duml.FLAG_REQUEST,
        duml.CMD_SET_GIMBAL, duml.GIMBAL_SET_MODE,
        duml.gimbal_mode_payload(duml.GIMBAL_MODE_LOCK),
    )
    assert duml.verify(frame)


def test_frame_layout_is_exact():
    # A pairing frame with a known seq, checked byte by byte against the spec.
    frame = duml.build_message(
        duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
        duml.CMD_SET_WIFI, duml.WIFI_SET_PAIRING_PIN,
        duml.pairing_payload("abc", "love"), seq=0x0100,
    )
    assert frame[0] == 0x55
    total = 13 + len(duml.pairing_payload("abc", "love"))
    assert frame[1] == (total & 0xFF)
    assert (frame[2] & 0x03) == ((total >> 8) & 0x03)
    assert (frame[2] >> 2) == 1  # version
    assert frame[3] == duml.crc8(frame[:3])
    # target LE: 0x0702
    assert frame[4] == duml.ADDR_APP
    assert frame[5] == duml.ADDR_WIFI
    # seq BE
    assert frame[6:8] == (0x0100).to_bytes(2, "big")
    assert frame[8] == duml.FLAG_REQUEST
    assert frame[9] == duml.CMD_SET_WIFI
    assert frame[10] == duml.WIFI_SET_PAIRING_PIN
    assert duml.verify(frame)


def test_corrupted_frame_fails_verify():
    frame = bytearray(duml.build_message(
        duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
        duml.CMD_SET_WIFI, duml.WIFI_SET_PAIRING_PIN, b"\x01",
    ))
    frame[11] ^= 0xFF  # flip a payload byte
    assert not duml.verify(bytes(frame))


def test_sequence_counter_wraps():
    c = duml.SequenceCounter(0xFFFF)
    assert c.next() == 0xFFFF
    assert c.next() == 0x0000


def test_pack_string():
    assert duml.pack_string("love") == b"\x04love"
    assert duml.pack_string("") == b"\x00"


def test_parse_stream_reassembles_split_frames():
    a = duml.build_message(duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
                           duml.CMD_SET_WIFI, 0x45, b"\x01", seq=0x0100)
    b = duml.build_message(duml.TARGET_APP_TO_GIMBAL, duml.FLAG_REQUEST,
                           duml.CMD_SET_GIMBAL, 0x4C, b"\x00\x00", seq=0x0101)
    stream = a + b
    # Feed it split at an awkward boundary, mid-first-frame.
    msgs1, rem1 = duml.parse_stream(stream[:7])
    assert msgs1 == []
    msgs2, rem2 = duml.parse_stream(rem1 + stream[7:])
    assert len(msgs2) == 2
    assert msgs2[0].cmd_id == 0x45
    assert msgs2[1].cmd_id == 0x4C
    assert rem2 == b""


def test_parse_stream_resyncs_past_garbage():
    good = duml.build_message(duml.TARGET_APP_TO_WIFI, duml.FLAG_REQUEST,
                              duml.CMD_SET_WIFI, 0x45, b"\x01")
    msgs, rem = duml.parse_stream(b"\x00\xff\x12" + good)
    assert len(msgs) == 1
    assert msgs[0].cmd_id == 0x45
