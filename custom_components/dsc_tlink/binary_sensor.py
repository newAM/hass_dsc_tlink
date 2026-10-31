"""Binary sensor platform for the DSC TLink integration.

One binary sensor per programmed zone of each connected panel. Zone "on"
means the zone is open. Tamper/fault/bypass flags are exposed as attributes.

Also one read-only "Chime" binary sensor per partition, tracking the door
chime state (this panel firmware rejects remote chime writes, so it is
status-only).
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    SIGNAL_CONNECTION_STATE,
    SIGNAL_PARTITION_UPDATED,
    SIGNAL_SESSION_ADDED,
    SIGNAL_ZONE_UPDATED,
)
from .coordinator import DscTLinkCoordinator
from .tlink.state import PartitionState, ZoneState

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DSC TLink binary sensor entities."""
    coordinator: DscTLinkCoordinator = hass.data[DOMAIN][entry.entry_id]

    def add_session_entities(session_id: str) -> None:
        handle = coordinator.get_handle(session_id)
        if handle is None:
            return
        entities = [
            DscTLinkZone(coordinator, session_id, zone)
            for zone in handle.store.active_zones()
        ]
        entities += [
            DscTLinkChime(coordinator, session_id, partition)
            for partition in handle.state.partitions.values()
        ]
        async_add_entities(entities)

    coordinator.async_register_add_entities(add_session_entities)


class DscTLinkZone(BinarySensorEntity):
    """Representation of a DSC zone as a binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DscTLinkCoordinator,
        session_id: str,
        zone: ZoneState,
    ) -> None:
        """Initialize the binary sensor entity."""
        self._coordinator = coordinator
        self._session_id = session_id
        self._zone_number = zone.number
        self.entity_description = BinarySensorEntityDescription(
            key=f"{session_id}_zone_{zone.number}",
            name=zone.display_name,
        )
        self._attr_unique_id = f"{session_id}_zone_{zone.number}"
        device_info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": zone.display_name,
            "manufacturer": MANUFACTURER,
            "model": "Neo Zone",
        }
        # Link to the panel device by registry id; the deprecated
        # `via_device` identifiers form is removed in HA 2027.8.
        if (panel_device_id := coordinator.get_panel_device_id(session_id)) is not None:
            device_info["via_device_id"] = panel_device_id
        self._attr_device_info = device_info

    def _zone(self) -> ZoneState | None:
        return self._coordinator.get_zone(self._session_id, self._zone_number)

    @property
    def is_on(self) -> bool | None:
        """Return True if the zone is open."""
        zone = self._zone()
        if zone is None:
            return None
        return zone.is_open

    @property
    def available(self) -> bool:
        """Return True while the panel session is connected."""
        return self._coordinator.get_handle(self._session_id) is not None

    @property
    def extra_attributes(self) -> dict[str, object] | None:
        """Expose zone flags and partition assignment."""
        zone = self._zone()
        if zone is None:
            return None
        return {
            "bypassed": zone.is_bypassed,
            "tampered": zone.is_tampered,
            "faulted": zone.is_faulted,
            "partitions": zone.partitions,
        }

    async def async_added_to_hass(self) -> None:
        """Register dispatch listeners when added to hass."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ZONE_UPDATED}_{self._session_id}_{self._zone_number}",
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION_STATE,
                self._handle_connection_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SESSION_ADDED,
                self._handle_session_added,
            )
        )

    @callback
    def _handle_update(self, *args: object) -> None:
        """Handle a zone state update."""
        self.async_write_ha_state()

    @callback
    def _handle_session_added(self, session_id: str) -> None:
        """Handle a (re)connected panel session."""
        if session_id == self._session_id:
            self.async_write_ha_state()

    @callback
    def _handle_connection_state(self, session_id: str, connected: bool) -> None:
        """Handle a connection state change."""
        if session_id == self._session_id:
            self.async_write_ha_state()


class DscTLinkChime(BinarySensorEntity):
    """Read-only door chime status for one partition."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DscTLinkCoordinator,
        session_id: str,
        partition: PartitionState,
    ) -> None:
        """Initialize the chime binary sensor entity."""
        self._coordinator = coordinator
        self._session_id = session_id
        self._partition_number = partition.number
        self.entity_description = BinarySensorEntityDescription(
            key=f"{session_id}_partition_{partition.number}_chime",
            name="Chime",
        )
        self._attr_unique_id = f"{session_id}_partition_{partition.number}_chime"
        device_info: DeviceInfo = {
            # Same device as the partition's alarm control panel entity.
            "identifiers": {(DOMAIN, f"{session_id}_partition_{partition.number}")},
            "name": partition.display_name,
            "manufacturer": MANUFACTURER,
            "model": "Neo Partition",
        }
        if (panel_device_id := coordinator.get_panel_device_id(session_id)) is not None:
            device_info["via_device_id"] = panel_device_id
        self._attr_device_info = device_info

    def _partition(self) -> PartitionState | None:
        return self._coordinator.get_partition(self._session_id, self._partition_number)

    @property
    def is_on(self) -> bool | None:
        """Return True while the door chime is enabled."""
        partition = self._partition()
        if partition is None:
            return None
        return partition.chime_enabled

    @property
    def available(self) -> bool:
        """Return True while the panel session is connected."""
        return self._coordinator.get_handle(self._session_id) is not None

    async def async_added_to_hass(self) -> None:
        """Register dispatch listeners when added to hass."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_PARTITION_UPDATED}_{self._session_id}_{self._partition_number}",
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION_STATE,
                self._handle_connection_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SESSION_ADDED,
                self._handle_session_added,
            )
        )

    @callback
    def _handle_update(self, *args: object) -> None:
        """Handle a partition state update."""
        self.async_write_ha_state()

    @callback
    def _handle_session_added(self, session_id: str) -> None:
        """Handle a (re)connected panel session."""
        if session_id == self._session_id:
            self.async_write_ha_state()

    @callback
    def _handle_connection_state(self, session_id: str, connected: bool) -> None:
        """Handle a connection state change."""
        if session_id == self._session_id:
            self.async_write_ha_state()
