"""End-to-end tests: TLinkServer + FakePanel over a real TCP socket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from fake_panel import DEFAULT_TYPE2_ACCESS_CODE, FakePanel
from tlink import DEFAULT_PORT, ArmingMode, EncryptionConfig, TLinkServer, messages
from tlink.framing import compact_int
from tlink.session import PanelCommandError

PANEL_ID = "0001234501"


class ServerHarness:
    """A TLinkServer plus collected events."""

    def __init__(self) -> None:
        self.connected: list[str] = []
        self.disconnected: list[str] = []
        self.partition_changes: list[tuple[str, int]] = []
        self.zone_changes: list[tuple[str, int]] = []
        self._connected_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self.server = TLinkServer(
            host="127.0.0.1",
            port=0,
            encryption_config=EncryptionConfig(encryption_type=2),
            on_session_connected=self._on_connected,
            on_session_disconnected=self._on_disconnected,
            on_partition_change=lambda sid, number: self.partition_changes.append(
                (sid, number)
            ),
            on_zone_change=lambda sid, number: self.zone_changes.append((sid, number)),
        )

    def _on_connected(self, handle: object) -> None:
        self.connected.append(handle.session_id)  # type: ignore[attr-defined]
        self._connected_event.set()

    def _on_disconnected(self, session_id: str) -> None:
        self.disconnected.append(session_id)
        self._disconnected_event.set()

    async def start(self) -> None:
        await self.server.start()

    async def stop(self) -> None:
        await self.server.stop()

    async def wait_connected(self) -> str:
        await asyncio.wait_for(self._connected_event.wait(), timeout=10)
        return self.connected[0]

    async def wait_disconnected(self) -> str:
        await asyncio.wait_for(self._disconnected_event.wait(), timeout=10)
        return self.disconnected[0]


async def wait_for_condition(
    condition: Callable[[], bool], timeout: float = 10.0
) -> None:
    """Poll an (assert-style) lambda until it passes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    pytest.fail("condition not met within timeout")


@pytest.fixture
async def harness() -> AsyncIterator[ServerHarness]:
    harness = ServerHarness()
    await harness.start()
    yield harness
    await harness.stop()


@pytest.fixture
async def panel(harness: ServerHarness) -> AsyncIterator[FakePanel]:
    panel = FakePanel("127.0.0.1", harness.server.port)
    await panel.connect()
    await panel.handshake()
    run_task = asyncio.create_task(panel.run())
    harness.panel_run_task = run_task  # type: ignore[attr-defined]
    yield panel
    run_task.cancel()
    await panel.close()


@pytest.mark.usefixtures("panel")
async def test_full_session(harness: ServerHarness) -> None:
    session_id = await harness.wait_connected()
    assert session_id == PANEL_ID

    handle = harness.server.get(session_id)
    assert handle is not None
    state = handle.state

    # Capabilities from the initial sync.
    assert state.capabilities.max_zones == 8
    assert state.capabilities.max_partitions == 8

    # Labels: all 8 zones, partition 1 only (2-8 rejected by the fake panel).
    assert state.zones[1].name == "Front Door"
    assert state.zones[8].name == "Hallway Smoke Detector"
    assert state.partitions[1].name == "Home"
    assert set(state.partitions) == {1}

    # Initial statuses: disarmed/ready, all zones secure.
    assert state.partitions[1].status == "disarmed"
    assert state.partitions[1].is_ready
    assert not state.zones[3].is_open

    # All 8 zones are labelled, so all are entity-worthy.
    assert len(handle.store.active_zones()) == 8


async def test_zone_open_notification(harness: ServerHarness, panel: FakePanel) -> None:
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None

    await panel.set_zone_open(3, True)
    await wait_for_condition(lambda: handle.state.zones[3].is_open)
    assert (PANEL_ID, 3) in harness.zone_changes

    await panel.set_zone_open(3, False)
    await wait_for_condition(lambda: not handle.state.zones[3].is_open)


async def test_arm_disarm_commands(harness: ServerHarness, panel: FakePanel) -> None:
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None

    await handle.arm(1, ArmingMode.AWAY_ARM, "1234")
    await wait_for_condition(lambda: handle.state.partitions[1].status == "armed_away")
    assert panel.arm_modes[1] == ArmingMode.AWAY_ARM

    await handle.arm(1, ArmingMode.STAY_ARM, "1234")
    await wait_for_condition(lambda: handle.state.partitions[1].status == "armed_home")

    await handle.disarm(1, "1234")
    await wait_for_condition(lambda: handle.state.partitions[1].status == "disarmed")


@pytest.mark.usefixtures("panel")
async def test_command_error_path(harness: ServerHarness) -> None:
    """A panel rejection surfaces as PanelCommandError."""
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None
    with pytest.raises(PanelCommandError) as excinfo:
        await handle.arm(99, ArmingMode.AWAY_ARM, "1234")
    assert "Invalid partition" in str(excinfo.value)


async def test_bypass_command(harness: ServerHarness, panel: FakePanel) -> None:
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None

    await handle.bypass_zone(1, 5, True, "1234")
    await wait_for_condition(lambda: handle.state.zones[5].is_bypassed)
    assert 5 in panel.bypassed_zones

    await handle.bypass_zone(1, 5, False, "1234")
    await wait_for_condition(lambda: not handle.state.zones[5].is_bypassed)


async def test_chime_status_tracking(harness: ServerHarness, panel: FakePanel) -> None:
    """Chime state is tracked read-only from panel status and broadcasts.

    This firmware rejects every remote chime write, so chime is observed
    only: via the partition status flags (initial sync and periodic
    refresh) and the chime broadcast notification (keypad [*4] toggles).
    """
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None
    assert not handle.state.partitions[1].chime_enabled

    panel.chime_enabled = True
    await handle.refresh_state()
    assert handle.state.partitions[1].chime_enabled

    panel.chime_enabled = False
    await panel._send_notification(
        messages.CMD_NOTIFICATION_CHIME, compact_int(1) + b"\x00"
    )
    await wait_for_condition(lambda: not handle.state.partitions[1].chime_enabled)


async def test_replayed_stale_event_cleared_by_refresh(
    harness: ServerHarness, panel: FakePanel
) -> None:
    """A replayed open event latches the zone; refresh_state() clears it.

    Panels replay buffered events after reconnecting (e.g. following a
    Home Assistant restart). A stale open whose close never arrives would
    otherwise latch the zone until the next real event.
    """
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None

    # Zone 4 is closed on the panel; simulate a replayed stale open.
    await panel._send_notification(
        messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS,
        compact_int(4) + b"\x01",
    )
    await wait_for_condition(lambda: handle.state.zones[4].is_open)

    await handle.refresh_state()
    assert not handle.state.zones[4].is_open


async def test_unsolicited_zone_status_push_applies(
    harness: ServerHarness, panel: FakePanel
) -> None:
    """Standalone ModuleZoneStatus pushes are authoritative corrections."""
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None
    assert not handle.state.zones[6].is_open

    await panel._send_notification(
        messages.CMD_ZONE_STATUS,
        messages.compact_int(1)
        + messages.compact_int(8)
        + bytes((1,))
        + bytes((0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00)),
    )
    await wait_for_condition(lambda: handle.state.zones[6].is_open)
    await wait_for_condition(lambda: handle.state.zones[6].is_tampered)


async def test_multiple_message_packet_batch_applies(
    harness: ServerHarness, panel: FakePanel
) -> None:
    """Every event inside a MultipleMessagePacket batch is applied.

    The panel batches close-spaced events (a zone open immediately
    followed by its close); dropping the batch was the cause of motion
    sensors latching open until the periodic status refresh.
    """
    await harness.wait_connected()
    handle = harness.server.get(PANEL_ID)
    assert handle is not None
    changes_before = len(harness.zone_changes)

    await panel._send_multiple(
        [
            (
                messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS,
                compact_int(5) + b"\x01",
            ),
            (
                messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS,
                compact_int(5) + b"\x00",
            ),
        ]
    )
    await wait_for_condition(
        lambda: len(harness.zone_changes) >= changes_before + 2,
    )
    assert not handle.state.zones[5].is_open


async def test_disconnect(harness: ServerHarness, panel: FakePanel) -> None:
    await harness.wait_connected()
    assert PANEL_ID in harness.server.sessions

    await panel.close()
    session_id = await harness.wait_disconnected()
    assert session_id == PANEL_ID
    assert harness.server.sessions == {}


async def test_server_stop_with_connected_panel(
    harness: ServerHarness, panel: FakePanel
) -> None:
    """Stopping the server must close live panel sessions without hanging."""
    await harness.wait_connected()
    assert PANEL_ID in harness.server.sessions

    await asyncio.wait_for(harness.stop(), timeout=10)
    assert harness.server.sessions == {}
    assert harness.disconnected == [PANEL_ID]
    # The panel side sees the connection drop.
    await asyncio.wait_for(panel.disconnected.wait(), timeout=10)
    # Mark the harness server stopped so the fixture's stop() is a no-op
    # (stop() is idempotent: _server is None after the first stop).
    harness.server._server = None


async def test_wrong_access_code_rejected(harness: ServerHarness) -> None:
    intruder = FakePanel(
        "127.0.0.1",
        harness.server.port,
        access_code="ffffffffffffffffffffffffffffffff",
    )
    await intruder.connect()
    with pytest.raises((ValueError, ConnectionError, AssertionError)):
        await asyncio.wait_for(intruder.handshake(), timeout=5)
    await intruder.close()
    await asyncio.sleep(0.2)
    assert harness.server.sessions == {}
    assert harness.connected == []


async def test_type1_encryption_session() -> None:
    harness = ServerHarness()
    harness.server.encryption_config = EncryptionConfig(
        encryption_type=1,
        access_code="42342342",
        integration_id="11111111",
    )
    await harness.start()
    try:
        panel = FakePanel(
            "127.0.0.1",
            harness.server.port,
            access_code="42342342",
            encryption_type=1,
            integration_id_code="11111111",
        )
        await panel.connect()
        await panel.handshake()
        run_task = asyncio.create_task(panel.run())
        try:
            session_id = await harness.wait_connected()
            assert session_id == PANEL_ID
            handle = harness.server.get(session_id)
            assert handle is not None
            await handle.arm(1, ArmingMode.NIGHT_ARM, "1234")
            await wait_for_condition(
                lambda: handle.state.partitions[1].status == "armed_night"
            )
        finally:
            run_task.cancel()
            await panel.close()
    finally:
        await harness.stop()


async def test_default_port_constant() -> None:
    assert DEFAULT_PORT == 3072
    assert DEFAULT_TYPE2_ACCESS_CODE == "12345678123456781234567812345678"
