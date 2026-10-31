"""Tests for panel state tracking."""

from __future__ import annotations

from tlink import messages
from tlink.framing import compact_int
from tlink.state import (
    STATUS_ARMED_AWAY,
    STATUS_ARMED_HOME,
    STATUS_ARMED_NIGHT,
    STATUS_ARMING,
    STATUS_DISARMED,
    STATUS_PENDING,
    STATUS_TRIGGERED,
    PanelStateStore,
    status_from_arming_mode,
    status_from_flags,
    zone_label_to_name,
)


def make_store() -> PanelStateStore:
    store = PanelStateStore("test-panel")
    changes: list[tuple[str, int]] = []
    store.on_partition_change = lambda p: changes.append(("partition", p.number))
    store.on_zone_change = lambda z: changes.append(("zone", z.number))
    store.changes = changes  # type: ignore[attr-defined]
    return store


def test_status_from_flags_mapping() -> None:
    assert status_from_flags(0x00, 0x01) == STATUS_TRIGGERED
    assert status_from_flags(0x40, 0x00) == STATUS_PENDING
    # Stay armed (0x03)
    assert status_from_flags(0x03, 0x00) == STATUS_ARMED_HOME
    # Night armed (0x09)
    assert status_from_flags(0x09, 0x00) == STATUS_ARMED_NIGHT
    # Armed away (0x01 or 0x05)
    assert status_from_flags(0x01, 0x00) == STATUS_ARMED_AWAY
    assert status_from_flags(0x05, 0x00) == STATUS_ARMED_AWAY
    # Disarmed
    assert status_from_flags(0x02, 0x00) == STATUS_DISARMED


def test_status_from_arming_mode() -> None:
    assert status_from_arming_mode(messages.ArmingMode.DISARM) == STATUS_DISARMED
    assert status_from_arming_mode(messages.ArmingMode.AWAY_ARM) == STATUS_ARMED_AWAY
    assert status_from_arming_mode(messages.ArmingMode.STAY_ARM) == STATUS_ARMED_HOME
    assert status_from_arming_mode(messages.ArmingMode.NIGHT_ARM) == STATUS_ARMED_NIGHT
    assert (
        status_from_arming_mode(messages.ArmingMode.ARM_WITH_NO_ENTRY_DELAY)
        == STATUS_ARMED_AWAY
    )


def test_zone_label_two_lines() -> None:
    label = "Front Door     Window          "  # 28 chars, two 14-char lines
    assert zone_label_to_name(label) == "Front Door Window"
    assert zone_label_to_name("  " * 28) == ""


def test_lifestyle_zone_updates_open_state() -> None:
    store = make_store()
    store.apply(
        messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS, compact_int(3) + b"\x01"
    )
    assert store.state.zones[3].is_open
    assert ("zone", 3) in store.changes  # type: ignore[attr-defined]
    store.apply(
        messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS, compact_int(3) + b"\x00"
    )
    assert not store.state.zones[3].is_open


def test_arm_disarm_notification_updates_status() -> None:
    store = make_store()
    store.apply(
        messages.CMD_NOTIFICATION_ARM_DISARM,
        compact_int(1) + bytes((messages.ArmingMode.AWAY_ARM, 1)) + compact_int(1),
    )
    assert store.state.partitions[1].status == STATUS_ARMED_AWAY
    store.apply(
        messages.CMD_NOTIFICATION_ARM_DISARM,
        compact_int(1) + bytes((messages.ArmingMode.DISARM, 1)) + compact_int(1),
    )
    assert store.state.partitions[1].status == STATUS_DISARMED


def test_exit_delay_sets_arming_then_cleared() -> None:
    store = make_store()
    store.apply(
        messages.CMD_NOTIFICATION_EXIT_DELAY,
        compact_int(1) + b"\x81" + compact_int(60),
    )
    partition = store.state.partitions[1]
    assert partition.status == STATUS_ARMING
    assert partition.exit_delay_active
    assert partition.exit_delay_seconds == 60
    # Delay finished
    store.apply(
        messages.CMD_NOTIFICATION_EXIT_DELAY,
        compact_int(1) + b"\x00" + compact_int(0),
    )
    partition = store.state.partitions[1]
    assert not partition.exit_delay_active
    assert partition.status == STATUS_DISARMED


def test_ready_status_forces_disarmed() -> None:
    store = make_store()
    store.apply(
        messages.CMD_NOTIFICATION_ARM_DISARM,
        compact_int(1) + bytes((messages.ArmingMode.STAY_ARM, 1)) + compact_int(1),
    )
    assert store.state.partitions[1].status == STATUS_ARMED_HOME
    # Panel cancelled the arm: ready status arrives and resets state.
    store.apply(
        messages.CMD_NOTIFICATION_PARTITION_READY_STATUS, compact_int(1) + b"\x01"
    )
    partition = store.state.partitions[1]
    assert partition.status == STATUS_DISARMED
    assert partition.is_ready


def test_bypass_notification() -> None:
    store = make_store()
    store.apply(messages.CMD_SINGLE_ZONE_BYPASS_STATUS, compact_int(5) + b"\x01")
    assert store.state.zones[5].is_bypassed


def test_label_response_updates_names() -> None:
    store = make_store()
    store.apply(
        messages.CMD_NOTIFICATION_LABEL_TEXT,
        compact_int(messages.LabelCollection.ZONE)
        + compact_int(1)
        + compact_int(2)
        + compact_int(56)  # 28 UTF-16BE bytes per label
        + "Front Door".encode("utf-16-be").ljust(56, b"\x00")
        + "Back Door".encode("utf-16-be").ljust(56, b"\x00"),
    )
    assert store.state.zones[1].name == "Front Door"
    assert store.state.zones[2].name == "Back Door"

    store.apply(
        messages.CMD_NOTIFICATION_LABEL_TEXT,
        compact_int(messages.LabelCollection.PARTITION)
        + compact_int(1)
        + compact_int(1)
        + compact_int(8)
        + "Home".encode("utf-16-be").ljust(8, b"\x00"),
    )
    assert store.state.partitions[1].name == "Home"


def test_bulk_zone_status_response() -> None:
    store = make_store()
    store.apply_zone_status_response(
        messages.ZoneStatusResponse(1, [0x01, 0x80, 0x00, 0x26])
    )
    zones = store.state.zones
    assert zones[1].is_open
    assert zones[2].is_bypassed
    assert not zones[3].is_open
    assert zones[4].is_tampered
    assert zones[4].is_faulted
    assert zones[1].partitions == [1]


def test_bulk_partition_status_response() -> None:
    store = make_store()
    store.apply_partition_status_response(
        messages.PartitionStatusResponse(1, 0x05, 0x00)
    )
    partition = store.state.partitions[1]
    assert partition.status == STATUS_ARMED_AWAY
    assert partition.is_ready is False


def test_partition_chime_from_status_flags() -> None:
    store = make_store()
    store.apply_partition_status_response(
        messages.PartitionStatusResponse(
            1, 0x02, messages.PartitionStatus2Flag.DOOR_CHIME_ENABLED
        )
    )
    assert store.state.partitions[1].chime_enabled

    store.apply_partition_status_response(
        messages.PartitionStatusResponse(1, 0x02, 0x00)
    )
    assert not store.state.partitions[1].chime_enabled


def test_chime_notification_updates_partition() -> None:
    store = make_store()
    store.apply(messages.CMD_NOTIFICATION_CHIME, compact_int(1) + b"\x01")
    assert store.state.partitions[1].chime_enabled
    store.apply(messages.CMD_NOTIFICATION_CHIME, compact_int(1) + b"\x00")
    assert not store.state.partitions[1].chime_enabled


def test_active_zones_heuristic() -> None:
    store = make_store()
    store.apply_zone_status_response(messages.ZoneStatusResponse(1, [0x00] * 8))
    # Nothing labelled, all secure: no zone deserves an entity.
    assert store.active_zones() == []
    store.state.zones[3].name = "Front Door"
    store.apply(
        messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS, compact_int(7) + b"\x01"
    )
    active = {zone.number for zone in store.active_zones()}
    assert active == {3, 7}


def test_malformed_notification_ignored() -> None:
    store = make_store()
    store.apply(messages.CMD_NOTIFICATION_ARM_DISARM, b"\xff\xff\xff")  # truncated
    assert store.state.partitions == {}


def test_unknown_command_ignored() -> None:
    store = make_store()
    store.apply(0x0220, b"\x01\x02")  # temperature broadcast
    assert store.state.zones == {}


def test_snapshot_is_copy() -> None:
    store = make_store()
    store.zone(1).name = "Front Door"
    snapshot = store.snapshot()
    snapshot.zones[1].name = "Changed"
    assert store.state.zones[1].name == "Front Door"
