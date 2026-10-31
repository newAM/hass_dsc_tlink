"""TLink server: accepts panel connections and manages sessions.

The DSC panel is always the client - it is programmed (installer section
[851]) to connect to this host on the configured port. For each accepted
connection the server:

1. runs the ITv2 handshake (creating a fresh encryption context),
2. starts the session receive loop (acking and dispatching notifications),
3. performs the initial state sync - capabilities, zone/partition labels,
   partition statuses, bulk zone status (ported from the NeoHub C#
   ``PanelConfigurationHandler`` fallback path, minus installer-config reads),
4. fires ``on_session_connected`` with a :class:`PanelHandle`,
5. waits for disconnect, firing ``on_session_disconnected``.

State changes surface through the ``on_partition_change`` /
``on_zone_change`` callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Callable

from . import messages
from .crypto import Encryption, EncryptionError, Type1Encryption, Type2Encryption
from .session import (
    HANDSHAKE_TIMEOUT,
    STREAM_LIMIT,
    PanelCommandError,
    PanelSession,
    SessionClosedError,
)
from .state import PanelStateStore, PartitionState, SessionState, ZoneState

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 3072
DEFAULT_TYPE2_ACCESS_CODE = "12345678123456781234567812345678"
LABEL_CHUNK_SIZE = 4  # MaxLabelsPerRequest in the C# implementation
REFRESH_INTERVAL = 60  # authoritative status re-pull period, in seconds


async def _pull_partition_statuses(
    session: PanelSession, store: PanelStateStore, max_partitions: int
) -> None:
    for partition in range(1, max_partitions + 1):
        try:
            data = await session.query(
                messages.CMD_PARTITION_STATUS,
                messages.encode_partition_status_request(partition),
            )
        except PanelCommandError as err:
            # Disabled/unconfigured partitions are rejected by the panel.
            _LOGGER.debug("Partition %s status unavailable: %s", partition, err)
            continue
        store.apply_partition_status_response(messages.decode_partition_status(data))


async def _pull_zone_status(
    session: PanelSession, store: PanelStateStore, max_zones: int
) -> None:
    data = await session.query(
        messages.CMD_ZONE_STATUS,
        messages.encode_zone_status_request(1, max_zones),
    )
    store.apply_zone_status_response(messages.decode_zone_status(data))


class EncryptionConfig:
    """Credentials for the ITv2 handshake with connecting panels."""

    def __init__(
        self,
        encryption_type: int = 2,
        access_code: str = DEFAULT_TYPE2_ACCESS_CODE,
        integration_id: str | None = None,
    ) -> None:
        self.encryption_type = encryption_type
        self.access_code = access_code
        self.integration_id = integration_id

    def build(self) -> Encryption:
        """Create a fresh encryption context for one handshake."""
        if self.encryption_type == 1:
            if not self.integration_id:
                msg = "Integration ID is required for encryption type 1"
                raise EncryptionError(msg)
            return Type1Encryption(self.access_code, self.integration_id)
        return Type2Encryption(self.access_code)


class PanelHandle:
    """Control API for one connected panel."""

    def __init__(
        self, session_id: str, session: PanelSession, store: PanelStateStore
    ) -> None:
        self.session_id = session_id
        self._session = session
        self.store = store

    @property
    def state(self) -> SessionState:
        return self.store.state

    async def close(self) -> None:
        """Close the underlying panel session."""
        await self._session.close()

    async def arm(self, partition: int, arm_mode: int, access_code: str = "") -> None:
        """Arm a partition in the given mode."""
        await self._session.command(
            messages.CMD_PARTITION_ARM,
            messages.encode_arm(partition, arm_mode, access_code),
        )

    async def disarm(self, partition: int, access_code: str) -> None:
        """Disarm a partition."""
        await self._session.command(
            messages.CMD_PARTITION_DISARM,
            messages.encode_disarm(partition, access_code),
        )

    async def bypass_zone(
        self, partition: int, zone: int, bypass: bool, access_code: str
    ) -> None:
        """Bypass or restore a single zone via bypass programming mode."""
        session = self._session
        await session.command(
            messages.CMD_CONFIGURATION_ENTER,
            messages.encode_config_enter(partition, access_code),
        )
        try:
            await session.command(
                messages.CMD_SINGLE_ZONE_BYPASS_WRITE,
                messages.encode_bypass_write(partition, zone, bypass),
            )
        finally:
            # Always exit programming mode, even if the write failed.
            await session.command(
                messages.CMD_CONFIGURATION_EXIT,
                messages.encode_config_exit(partition),
            )

    async def refresh_state(self) -> None:
        """Re-pull authoritative partition and zone statuses.

        The panel replays buffered events after reconnecting; a replayed
        stale event can latch a zone open even though the initial sync
        already ran. This is the eventual-consistency backstop: the same
        authoritative pulls as the initial sync, callable on demand and
        periodically from the server's refresh loop.
        """
        capabilities = self.store.state.capabilities
        await _pull_partition_statuses(
            self._session, self.store, capabilities.max_partitions
        )
        await _pull_zone_status(self._session, self.store, capabilities.max_zones)


class TLinkServer:
    """Listens for panel connections on one TCP port."""

    def __init__(
        self,
        host: str = "0.0.0.0",  # noqa: S104 - panels connect from the LAN
        port: int = DEFAULT_PORT,
        encryption_config: EncryptionConfig | None = None,
        *,
        on_session_connected: Callable[[PanelHandle], None] | None = None,
        on_session_disconnected: Callable[[str], None] | None = None,
        on_partition_change: Callable[[str, int], None] | None = None,
        on_zone_change: Callable[[str, int], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.encryption_config = encryption_config or EncryptionConfig()
        self.on_session_connected = on_session_connected
        self.on_session_disconnected = on_session_disconnected
        self.on_partition_change = on_partition_change
        self.on_zone_change = on_zone_change
        self._server: asyncio.Server | None = None
        self._sessions: dict[str, PanelHandle] = {}

    # --- lifecycle ---
    async def start(self) -> None:
        """Start listening. Raises ``OSError`` if the port is in use."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
            limit=STREAM_LIMIT,
        )
        bound_port = self._server.sockets[0].getsockname()[1]
        if self.port == 0:
            self.port = bound_port
        _LOGGER.info("TLink server listening on %s:%s", self.host, bound_port)

    async def stop(self) -> None:
        """Stop listening and close all panel sessions."""
        if self._server is not None:
            self._server.close()  # stop accepting new connections
        for handle in list(self._sessions.values()):
            await self._close_session(handle.session_id, handle)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    @property
    def sessions(self) -> dict[str, PanelHandle]:
        """Currently connected panels keyed by session id."""
        return dict(self._sessions)

    def get(self, session_id: str) -> PanelHandle | None:
        return self._sessions.get(session_id)

    # --- connection handling ---
    async def _handshake(self, session: PanelSession, peer: tuple) -> str | None:
        """Run the ITv2 handshake; returns the session id or None on failure."""
        try:
            session_id = await asyncio.wait_for(
                session.handshake(), timeout=HANDSHAKE_TIMEOUT
            )
        except Exception as err:  # noqa: BLE001 - logged and dropped
            _LOGGER.warning(
                "Handshake with %s failed: %s "
                "(check the encryption type and access code)",
                peer,
                err,
            )
            return None
        return session_id or f"panel-{peer[0]}"

    def _make_store(self, session_id: str) -> PanelStateStore:
        """Create the state store for a session and wire notifications."""
        store = PanelStateStore(session_id)
        store.on_partition_change = self._make_partition_notifier(session_id)
        store.on_zone_change = self._make_zone_notifier(session_id)
        return store

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        # Panels pace bursts of events on our protocol acks; Nagle would
        # add tens of milliseconds of delay to those small ack writes.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        session = PanelSession(reader, writer, self.encryption_config.build())
        handle: PanelHandle | None = None
        run_task: asyncio.Task[None] | None = None
        refresh_task: asyncio.Task[None] | None = None
        try:
            session_id = await self._handshake(session, peer)
            if session_id is None:
                return

            store = self._make_store(session_id)
            session.on_notification = store.apply
            session.start_keepalive()

            run_task = asyncio.create_task(session.run(), name=f"tlink-{session_id}")
            try:
                await self._initial_sync(session, store)
            except Exception as err:  # noqa: BLE001 - panel unusable
                _LOGGER.warning("Initial sync with %s failed: %s", session_id, err)
            else:
                handle = PanelHandle(session_id, session, store)
                self._sessions[session_id] = handle
                refresh_task = asyncio.create_task(
                    self._refresh_loop(session, store),
                    name=f"tlink-refresh-{session_id}",
                )
                _LOGGER.info(
                    "Panel %s connected (%s zones, %s partitions)",
                    session_id,
                    store.state.capabilities.max_zones,
                    store.state.capabilities.max_partitions,
                )
                if self.on_session_connected is not None:
                    self.on_session_connected(handle)
                await run_task
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Panel connection error", exc_info=True)
        finally:
            await self._teardown_connection(session, handle, run_task, refresh_task)

    async def _teardown_connection(
        self,
        session: PanelSession,
        handle: PanelHandle | None,
        *tasks: asyncio.Task[None] | None,
    ) -> None:
        """Cancel session tasks, close the transport, drop the session."""
        for task in tasks:
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await session.close()
        if handle is not None:
            await self._close_session(handle.session_id, handle)

    async def _refresh_loop(
        self, session: PanelSession, store: PanelStateStore
    ) -> None:
        """Periodically re-pull authoritative status.

        Missed or replayed events (the panel replays its buffer after a
        reconnect) can leave stale zone/partition state latched until the
        next event; the periodic pull bounds that window.
        """
        try:
            while True:
                await asyncio.sleep(REFRESH_INTERVAL)
                try:
                    capabilities = store.state.capabilities
                    await _pull_partition_statuses(
                        session, store, capabilities.max_partitions
                    )
                    await _pull_zone_status(session, store, capabilities.max_zones)
                except (PanelCommandError, TimeoutError) as err:
                    _LOGGER.debug("Status refresh failed (will retry): %s", err)
        except asyncio.CancelledError:
            raise
        except ConnectionError, OSError, SessionClosedError:
            _LOGGER.debug("Status refresh loop ended")

    async def _close_session(self, session_id: str, handle: PanelHandle) -> None:
        if self._sessions.get(session_id) is handle:
            del self._sessions[session_id]
            await handle.close()
            _LOGGER.info("Panel %s disconnected", session_id)
            if self.on_session_disconnected is not None:
                self.on_session_disconnected(session_id)

    # --- initial sync (ported from C# PanelConfigurationHandler) ---
    async def _initial_sync(
        self, session: PanelSession, store: PanelStateStore
    ) -> None:
        caps_data = await session.query(
            messages.CMD_SYSTEM_CAPABILITIES,
            messages.encode_capabilities_request(),
        )
        capabilities = messages.decode_capabilities(caps_data)
        store.state.capabilities = capabilities
        _LOGGER.info(
            "Panel capabilities: %s zones, %s partitions, %s users",
            capabilities.max_zones,
            capabilities.max_partitions,
            capabilities.max_users,
        )

        await self._pull_labels(
            session, store, messages.LabelCollection.ZONE, capabilities.max_zones
        )
        await self._pull_labels(
            session,
            store,
            messages.LabelCollection.PARTITION,
            capabilities.max_partitions,
        )
        await _pull_partition_statuses(session, store, capabilities.max_partitions)
        await _pull_zone_status(session, store, capabilities.max_zones)

    async def _pull_labels(
        self,
        session: PanelSession,
        store: PanelStateStore,
        collection: int,
        count: int,
    ) -> None:
        for start in range(1, count + 1, LABEL_CHUNK_SIZE):
            end = min(start + LABEL_CHUNK_SIZE - 1, count)
            try:
                data = await session.query(
                    messages.CMD_NOTIFICATION_LABEL_TEXT,
                    messages.encode_label_request(collection, start, end),
                )
            except PanelCommandError, TimeoutError:
                _LOGGER.debug(
                    "Label request %s-%s (collection 0x%02X) failed",
                    start,
                    end,
                    collection,
                )
                continue
            store.apply_label_response(messages.decode_label_text(data))

    # --- change notification plumbing ---
    def _make_partition_notifier(
        self, session_id: str
    ) -> Callable[[PartitionState], None]:
        def notify(partition_state: PartitionState) -> None:
            if self.on_partition_change is not None:
                self.on_partition_change(session_id, partition_state.number)

        return notify

    def _make_zone_notifier(self, session_id: str) -> Callable[[ZoneState], None]:
        def notify(zone_state: ZoneState) -> None:
            if self.on_zone_change is not None:
                self.on_zone_change(session_id, zone_state.number)

        return notify


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TYPE2_ACCESS_CODE",
    "EncryptionConfig",
    "PanelCommandError",
    "PanelHandle",
    "SessionClosedError",
    "TLinkServer",
]
