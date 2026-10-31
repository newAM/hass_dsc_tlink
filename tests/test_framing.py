"""Tests for TLink transport and ITv2 framing."""

from __future__ import annotations

import pytest
from tlink.framing import (
    TLinkDecoder,
    add_framing,
    compact_int,
    crc16,
    decode_tlink,
    encode_tlink,
    parse_compact_int,
    remove_framing,
    stuff,
    unstuff,
)


def test_crc16_check_value() -> None:
    # Check value for poly 0x1021, init 0xFFFF, no reflection
    # (CRC-16/CCITT-FALSE parameters) - what the panel uses.
    assert crc16(b"123456789") == 0x29B1


def test_crc16_empty() -> None:
    assert crc16(b"") == 0xFFFF


def test_stuff_unstuff_roundtrip() -> None:
    for data in (b"", b"hello", bytes(range(256))):
        assert unstuff(stuff(data)) == data


def test_stuff_escapes_delimiters() -> None:
    stuffed = stuff(b"\x7d\x7e\x7f")
    assert stuffed == b"\x7d\x00\x7d\x01\x7d\x02"


def test_unstuff_dangling_escape() -> None:
    with pytest.raises(ValueError, match="dangling"):
        unstuff(b"\x01\x7d")


def test_tlink_encode_decode_roundtrip() -> None:
    header = b"0001234501"
    payload = bytes(range(256))
    raw = encode_tlink(header, payload)
    assert raw[-1] == 0x7F
    assert decode_tlink(raw) == (header, payload)


def test_framing_roundtrip_short() -> None:
    message = b"\x01\x02\x08\x11data"
    framed = add_framing(message)
    assert framed[0] < 0x80  # single-byte length
    assert remove_framing(framed) == message


def test_framing_roundtrip_long() -> None:
    message = b"x" * 300
    framed = add_framing(message)
    assert framed[0] & 0x80  # two-byte length
    assert remove_framing(framed) == message


def test_framing_crc_mismatch() -> None:
    framed = bytearray(add_framing(b"payload"))
    framed[-1] ^= 0xFF
    with pytest.raises(ValueError, match="CRC"):
        remove_framing(bytes(framed))


def test_compact_int_values() -> None:
    assert compact_int(0) == b"\x01\x00"
    assert compact_int(1) == b"\x01\x01"
    assert compact_int(127) == b"\x01\x7f"
    assert compact_int(256) == b"\x02\x01\x00"
    assert compact_int(65535) == b"\x02\xff\xff"


def test_compact_int_roundtrip() -> None:
    for value in (0, 1, 9, 42, 255, 256, 4096, 65535, 1_000_000):
        encoded = compact_int(value)
        parsed, offset = parse_compact_int(encoded)
        assert parsed == value
        assert offset == len(encoded)


def test_parse_compact_int_truncated() -> None:
    with pytest.raises(ValueError, match="past"):
        parse_compact_int(b"\x02\x01")


def test_decoder_splits_stream() -> None:
    decoder = TLinkDecoder()
    packet1 = encode_tlink(b"header", b"one")
    packet2 = encode_tlink(b"header", b"two")
    # Feed everything as one jumbled chunk plus a partial packet.
    received = list(decoder.feed(packet1 + packet2[:5]))
    assert received == [packet1]
    received = list(decoder.feed(packet2[5:]))
    assert received == [packet2]
    assert decoder.pending == b""
