"""Coordinator for the DSC TLink integration.

Owns the :class:`tlink.TLinkServer` and bridges its callbacks into Home
Assistant dispatcher signals:

* ``SIGNAL_SESSION_ADDED``         - a panel finished its initial sync
* ``SIGNAL_CONNECTION_STATE``      - a panel connected/disconnected
* ``SIGNAL_PARTITION_UPDATED``     - one partition changed
* ``SIGNAL_ZONE_UPDATED``          - one zone changed

Panels connect at any time after setup, so platforms register
"add entities" callbacks here and new sessions' entities are created
dynamically when a panel completes its initial sync.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ACCESS_CODE,
    CONF_ENCRYPTION_TYPE,
    CONF_INTEGRATION_ID,
    CONF_PANEL_CODE,
    DOMAIN,
    MANUFACTURER,
    PANEL_MODEL,
    SIGNAL_CONNECTION_STATE,
    SIGNAL_PARTITION_UPDATED,
    SIGNAL_SESSION_ADDED,
    SIGNAL_ZONE_UPDATED,
)
from .tlink import EncryptionConfig, TLinkServer
from .tlink.state import PartitionState, ZoneState

if TYPE_CHECKING:
    from .tlink import PanelHandle

_LOGGER = logging.getLogger(__name__)

AddEntitiesForSession = Callable[[str], None]


class DscTLinkCoordinator:
    """Manage the TLink server for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._add_entities_callbacks: list[AddEntitiesForSession] = []
        self._sessions_with_entities: set[str] = set()
        self._panel_code: str = entry.data.get(CONF_PANEL_CODE) or ""
        self.server = TLinkServer(
            port=entry.data[CONF_PORT],
            encryption_config=EncryptionConfig(
                encryption_type=int(entry.data[CONF_ENCRYPTION_TYPE]),
                access_code=entry.data[CONF_ACCESS_CODE],
                integration_id=entry.data.get(CONF_INTEGRATION_ID),
            ),
            on_session_connected=self._handle_session_connected,
            on_session_disconnected=self._handle_session_disconnected,
            on_partition_change=self._handle_partition_change,
            on_zone_change=self._handle_zone_change,
        )

    # --- lifecycle ---
    async def async_start(self) -> None:
        """Start listening for panels.

        Raises ``OSError`` (e.g. ``EADDRINUSE``) if the port is taken; the
        caller translates that into ``ConfigEntryNotReady``.
        """
        await self.server.start()

    async def async_shutdown(self) -> None:
        """Stop the server and drop all panel sessions."""
        await self.server.stop()

    # --- state access for entities ---
    def get_handle(self, session_id: str) -> PanelHandle | None:
        """Return the handle for a connected panel, if any."""
        return self.server.get(session_id)

    def get_panel_device_id(self, session_id: str) -> str | None:
        """Return the device registry id of the panel's device, if known.

        The panel device is registered before session entities are created,
        so zone/partition devices can link to it via ``via_device_id``.
        """
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, session_id), self.entry.entry_id
        )
        return device.id if device is not None else None

    def get_partition(self, session_id: str, partition: int) -> PartitionState | None:
        handle = self.server.get(session_id)
        if handle is None:
            return None
        return handle.state.partitions.get(partition)

    def get_zone(self, session_id: str, zone: int) -> ZoneState | None:
        handle = self.server.get(session_id)
        if handle is None:
            return None
        return handle.state.zones.get(zone)

    # --- commands from entities ---
    async def async_arm(
        self, session_id: str, partition: int, arm_mode: int, code: str | None
    ) -> None:
        handle = self.server.get(session_id)
        if handle is None:
            raise RuntimeError("Panel is not connected")
        await handle.arm(partition, arm_mode, code or self._panel_code)

    async def async_disarm(
        self, session_id: str, partition: int, code: str | None
    ) -> None:
        handle = self.server.get(session_id)
        if handle is None:
            raise RuntimeError("Panel is not connected")
        await handle.disarm(partition, code or self._panel_code)

    # --- platform hookup ---
    @callback
    def async_register_add_entities(self, callback_: AddEntitiesForSession) -> None:
        """Register a platform callback that creates entities for a session."""
        self._add_entities_callbacks.append(callback_)
        # Platforms register late in entry setup; catch them up on any
        # sessions that connected in the meantime.
        for handle in self.server.sessions.values():
            self._add_entities_for(handle.session_id)

    def _add_entities_for(self, session_id: str) -> None:
        if session_id in self._sessions_with_entities:
            return
        self._sessions_with_entities.add(session_id)
        for add_entities in self._add_entities_callbacks:
            add_entities(session_id)

    # --- server callbacks ---
    @callback
    def _handle_session_connected(self, handle: PanelHandle) -> None:
        _LOGGER.debug("Session connected: %s", handle.session_id)
        device_registry = dr.async_get(self.hass)
        device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, handle.session_id)},
            name=f"DSC Panel {handle.session_id}",
            manufacturer=MANUFACTURER,
            model=PANEL_MODEL,
        )
        self._add_entities_for(handle.session_id)
        async_dispatcher_send(self.hass, SIGNAL_SESSION_ADDED, handle.session_id)
        async_dispatcher_send(
            self.hass, SIGNAL_CONNECTION_STATE, handle.session_id, True
        )

    @callback
    def _handle_session_disconnected(self, session_id: str) -> None:
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION_STATE, session_id, False)

    @callback
    def _handle_partition_change(self, session_id: str, partition: int) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_PARTITION_UPDATED}_{session_id}_{partition}"
        )

    @callback
    def _handle_zone_change(self, session_id: str, zone: int) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_ZONE_UPDATED}_{session_id}_{zone}")
