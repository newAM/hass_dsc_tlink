"""Alarm control panel platform for the DSC TLink integration.

One alarm control panel entity per partition of each connected panel.
"""

from __future__ import annotations

import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
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
)
from .coordinator import DscTLinkCoordinator
from .tlink import ArmingMode
from .tlink.state import PartitionState

_LOGGER = logging.getLogger(__name__)

STATUS_MAP: dict[str, AlarmControlPanelState] = {
    "disarmed": AlarmControlPanelState.DISARMED,
    "armed_away": AlarmControlPanelState.ARMED_AWAY,
    "armed_home": AlarmControlPanelState.ARMED_HOME,
    "armed_night": AlarmControlPanelState.ARMED_NIGHT,
    "arming": AlarmControlPanelState.ARMING,
    "pending": AlarmControlPanelState.PENDING,
    "triggered": AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DSC TLink alarm control panel entities."""
    coordinator: DscTLinkCoordinator = hass.data[DOMAIN][entry.entry_id]

    def add_session_entities(session_id: str) -> None:
        handle = coordinator.get_handle(session_id)
        if handle is None:
            return
        entities = [
            DscTLinkPartition(coordinator, session_id, partition)
            for partition in handle.state.partitions.values()
        ]
        async_add_entities(entities)

    coordinator.async_register_add_entities(add_session_entities)


class DscTLinkPartition(AlarmControlPanelEntity):
    """Representation of a DSC partition as an alarm control panel."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    _attr_code_arm_required = False
    _attr_code_format = CodeFormat.NUMBER

    def __init__(
        self,
        coordinator: DscTLinkCoordinator,
        session_id: str,
        partition: PartitionState,
    ) -> None:
        """Initialize the alarm panel entity."""
        self._coordinator = coordinator
        self._session_id = session_id
        self._partition_number = partition.number
        self._attr_unique_id = f"{session_id}_partition_{partition.number}"
        device_info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": partition.display_name,
            "manufacturer": MANUFACTURER,
            "model": "Neo Partition",
        }
        # Link to the panel device by registry id; the deprecated
        # `via_device` identifiers form is removed in HA 2027.8.
        if (panel_device_id := coordinator.get_panel_device_id(session_id)) is not None:
            device_info["via_device_id"] = panel_device_id
        self._attr_device_info = device_info

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the current alarm state."""
        partition = self._coordinator.get_partition(
            self._session_id, self._partition_number
        )
        if partition is None:
            return None
        return STATUS_MAP.get(partition.status)

    @property
    def available(self) -> bool:
        """Return True while the panel session is connected."""
        return self._coordinator.get_handle(self._session_id) is not None

    @property
    def extra_attributes(self) -> dict[str, object] | None:
        """Expose exit-delay and readiness details."""
        partition = self._coordinator.get_partition(
            self._session_id, self._partition_number
        )
        if partition is None:
            return None
        return {
            "ready": partition.is_ready,
            "chime_enabled": partition.chime_enabled,
            "exit_delay_active": partition.exit_delay_active,
            "exit_delay_seconds": partition.exit_delay_seconds,
            "exit_delay_audible": partition.exit_delay_audible,
        }

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

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        await self._coordinator.async_arm(
            self._session_id, self._partition_number, ArmingMode.AWAY_ARM, code
        )

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home (stay) command."""
        await self._coordinator.async_arm(
            self._session_id, self._partition_number, ArmingMode.STAY_ARM, code
        )

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        await self._coordinator.async_arm(
            self._session_id, self._partition_number, ArmingMode.NIGHT_ARM, code
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        await self._coordinator.async_disarm(
            self._session_id, self._partition_number, code
        )
