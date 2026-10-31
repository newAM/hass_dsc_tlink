"""Tests for ITv2 message encoding and decoding."""

from __future__ import annotations

import pytest
from tlink import messages
from tlink.framing import compact_int


def test_bcd_encode() -> None:
    assert messages.bcd_encode("") == b""
    assert messages.bcd_encode("1") == b"\x10"
    assert messages.bcd_encode("12") == b"\x12"
    assert messages.bcd_encode("1234") == b"\x12\x34"
    assert messages.bcd_encode("123") == b"\x12\x30"
    assert messages.bcd_encode("AAAA") == b"\xaa\xaa"  # hex sentinel codes


def test_bcd_encode_invalid() -> None:
    with pytest.raises(ValueError, match="BCD"):
        messages.bcd_encode("12x4")


def test_bcd_decode() -> None:
    assert messages.bcd_decode(b"\x12\x34") == "1234"
    assert messages.bcd_decode(b"\x12\x30") == "123"


def test_digit_bytes_encode() -> None:
    assert messages.digit_bytes_encode("1234") == b"\x04\x01\x02\x03\x04"


def test_digit_bytes_encode_rejects_non_digits() -> None:
    with pytest.raises(ValueError, match="digits"):
        messages.digit_bytes_encode("12a4")


def test_encode_arm() -> None:
    assert messages.encode_arm(1, messages.ArmingMode.AWAY_ARM, "1234") == (
        compact_int(1) + b"\x02\x12\x34"
    )
    # Codeless arming sends no BCD bytes.
    assert messages.encode_arm(1, messages.ArmingMode.STAY_ARM) == (
        compact_int(1) + b"\x01"
    )


def test_encode_disarm() -> None:
    assert messages.encode_disarm(2, "1234") == compact_int(2) + b"\x12\x34"


def test_encode_config_enter() -> None:
    # [CompactInt partition][mode][len][digit bytes][read/write]
    encoded = messages.encode_config_enter(1, "1234")
    assert encoded == compact_int(1) + b"\x03\x04\x01\x02\x03\x04\x01"


def test_encode_bypass_write() -> None:
    assert messages.encode_bypass_write(1, 5, True) == (
        compact_int(1) + compact_int(5) + b"\x01"
    )


def test_decode_chime_notification() -> None:
    note = messages.parse_notification(
        messages.CMD_NOTIFICATION_CHIME, compact_int(1) + b"\x01"
    )
    assert note == messages.ChimeNotification(partition=1, enabled=True)


def test_multiple_message_packet_roundtrip() -> None:
    entries = [
        (messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS, compact_int(5) + b"\x01"),
        (messages.CMD_NOTIFICATION_ARM_DISARM, compact_int(1) + b"\x00\x01"),
    ]
    encoded = messages.encode_multiple_messages(entries)
    assert max(encoded) <= 0x7F  # all sub-messages use one-byte lengths
    assert messages.decode_multiple_messages(encoded) == entries

    # A sub-message over 0x7F bytes uses the two-byte length form.
    long_entries = [*entries, (messages.CMD_ZONE_STATUS, b"\x00" * 0x80)]
    long_encoded = messages.encode_multiple_messages(long_entries)
    assert messages.decode_multiple_messages(long_encoded) == long_entries


def test_encode_zone_status_request() -> None:
    assert messages.encode_zone_status_request(1, 8) == (
        compact_int(1) + compact_int(8) + b"\x00"
    )


def test_encode_capabilities_request() -> None:
    assert messages.encode_capabilities_request() == compact_int(0) * 6


def test_encode_label_request() -> None:
    assert messages.encode_label_request(messages.LabelCollection.ZONE, 1, 4) == (
        compact_int(0xD1) + compact_int(1) + compact_int(4)
    )


def test_decode_capabilities() -> None:
    data = (
        compact_int(48)
        + compact_int(95)
        + compact_int(8)
        + compact_int(4)
        + compact_int(16)
        + compact_int(14)
    )
    caps = messages.decode_capabilities(data)
    assert caps.max_zones == 48
    assert caps.max_users == 95
    assert caps.max_partitions == 8


def test_decode_zone_status() -> None:
    data = compact_int(1) + compact_int(4) + b"\x01" + bytes((0x01, 0x80, 0x00, 0x20))
    response = messages.decode_zone_status(data)
    assert response.zone_start == 1
    assert response.statuses == [0x01, 0x80, 0x00, 0x20]


def test_decode_partition_status() -> None:
    data = compact_int(1) + b"\x02\x05\x00"
    response = messages.decode_partition_status(data)
    assert response.partition == 1
    assert response.status1 == 0x05  # armed away
    assert response.status2 == 0x00


def test_decode_partition_status_short() -> None:
    # A single status byte must still decode.
    response = messages.decode_partition_status(compact_int(3) + b"\x01\x02")
    assert response.partition == 3
    assert response.status1 == 0x02
    assert response.status2 == 0x00


def _label_payload(labels: list[str], start: int = 1, end: int | None = None) -> bytes:
    end = end if end is not None else start + len(labels) - 1
    width = max(len(label.encode("utf-16-be")) for label in labels)
    data = (
        compact_int(messages.LabelCollection.ZONE)
        + compact_int(start)
        + compact_int(end)
        + compact_int(width)
    )
    for label in labels:
        encoded = label.encode("utf-16-be")
        data += encoded + b"\x00" * (width - len(encoded))
    return data


def test_decode_label_text() -> None:
    response = messages.decode_label_text(_label_payload(["Front Door", "Back Door"]))
    assert response.collection == messages.LabelCollection.ZONE
    assert response.start == 1
    assert response.labels == ["Front Door", "Back Door"]


def test_decode_label_text_unicode() -> None:
    response = messages.decode_label_text(_label_payload(["Tür 🚪"]))
    assert response.labels == ["Tür 🚪"]


def test_decode_lifestyle_zone() -> None:
    opened = messages.decode_lifestyle_zone(compact_int(3) + b"\x01")
    assert opened.zone == 3
    assert opened.is_open
    closed = messages.decode_lifestyle_zone(compact_int(3) + b"\x00")
    assert not closed.is_open


def test_decode_arm_disarm() -> None:
    notification = messages.decode_arm_disarm(
        compact_int(1) + b"\x02\x01" + compact_int(8)
    )
    assert notification.partition == 1
    assert notification.arm_mode == messages.ArmingMode.AWAY_ARM
    assert notification.method == 1
    assert notification.user_id == 8


def test_decode_exit_delay() -> None:
    notification = messages.decode_exit_delay(
        compact_int(1) + b"\x81" + compact_int(45)
    )
    assert notification.partition == 1
    assert notification.is_active
    assert notification.is_audible
    assert notification.duration_seconds == 45


def test_decode_ready_status() -> None:
    notification = messages.decode_ready_status(compact_int(1) + b"\x01")
    assert notification.partition == 1
    assert notification.ready_status == messages.ReadyStatus.READY_TO_ARM


def test_decode_bypass_status() -> None:
    notification = messages.decode_bypass_status(compact_int(5) + b"\x01")
    assert notification.zone == 5
    assert notification.is_bypassed


def test_parse_notification_routing() -> None:
    assert isinstance(
        messages.parse_notification(
            messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS,
            compact_int(1) + b"\x01",
        ),
        messages.LifestyleZoneNotification,
    )
    assert messages.parse_notification(0x9999, b"") is None


def test_response_code_descriptions() -> None:
    assert messages.ResponseCode.describe(0x00) == "Success"
    assert "access code" in messages.ResponseCode.describe(0x11).lower()
    assert "0x7F" in messages.ResponseCode.describe(0x7F)
