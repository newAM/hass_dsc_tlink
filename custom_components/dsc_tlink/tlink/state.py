"""Per-session panel state tracking.

Maintains zones and partitions for one connected panel, updated both by
real-time notifications (open/close, arm/disarm, exit delay, bypass, labels)
and by the authoritative bulk pulls done during the initial sync.

The state-mapping logic ports the NeoHub C# notification handlers
(``Services/Handlers/*``) - in particular: any partition-ready notification
is treated as "disarmed" (it clears stale armed/arming state when the panel
cancels an arm), and partition flag bytes map through a fixed priority order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from . import messages
from .messages import (
    ArmDisarmNotification,
    BypassStatusNotification,
    ChimeNotification,
    ExitDelayNotification,
    LabelCollection,
    LabelTextResponse,
    LifestyleZoneNotification,
    PartitionStatus2Flag,
    PartitionStatusResponse,
    ReadyStatus,
    ReadyStatusNotification,
    SystemCapabilities,
    ZoneStatusFlag,
    ZoneStatusResponse,
)

_LOGGER = logging.getLogger(__name__)

STATUS_DISARMED = "disarmed"
STATUS_ARMED_AWAY = "armed_away"
STATUS_ARMED_HOME = "armed_home"
STATUS_ARMED_NIGHT = "armed_night"
STATUS_ARMING = "arming"
STATUS_PENDING = "pending"
STATUS_TRIGGERED = "triggered"


@dataclass
class ZoneState:
    """One panel zone."""

    number: int
    name: str | None = None
    is_open: bool = False
    is_faulted: bool = False
    is_tampered: bool = False
    is_bypassed: bool = False
    partitions: list[int] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """Panel label (two 14-character lines joined), or a default."""
        if self.name:
            return self.name
        return f"Zone {self.number}"


@dataclass
class PartitionState:
    """One panel partition."""

    number: int
    name: str | None = None
    status: str = STATUS_DISARMED
    is_ready: bool | None = None
    exit_delay_active: bool = False
    exit_delay_seconds: int = 0
    exit_delay_audible: bool = False
    chime_enabled: bool = False

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        return f"Partition {self.number}"


def status_from_flags(status1: int, status2: int) -> str:
    """Map ``ModulePartitionStatus`` flag bytes to a status string.

    Stay/Night arming are combined flag values (ARMED | stay/night bit), so
    they must match with all bits set before the generic ARMED check.
    """
    if status2 & PartitionStatus2Flag.PARTITION_IN_ALARM:
        return STATUS_TRIGGERED
    if status1 & messages.PartitionStatus1Flag.ENTRY_DELAY_IN_PROGRESS:
        return STATUS_PENDING
    if status1 & messages.PartitionStatus1Flag.STAY_ARMED == (
        messages.PartitionStatus1Flag.STAY_ARMED
    ):
        return STATUS_ARMED_HOME
    if (
        status1 & messages.PartitionStatus1Flag.NIGHT_ARMED
        == messages.PartitionStatus1Flag.NIGHT_ARMED
    ):
        return STATUS_ARMED_NIGHT
    if status1 & messages.PartitionStatus1Flag.ARMED:
        return STATUS_ARMED_AWAY
    return STATUS_DISARMED


def status_from_arming_mode(arm_mode: int) -> str:
    """Map an ``ArmingMode`` from an arm/disarm notification."""
    if arm_mode == messages.ArmingMode.DISARM:
        return STATUS_DISARMED
    if arm_mode in (
        messages.ArmingMode.STAY_ARM,
        messages.ArmingMode.STAY_ARM_WITH_NO_ENTRY_DELAY,
    ):
        return STATUS_ARMED_HOME
    if arm_mode in (
        messages.ArmingMode.NIGHT_ARM,
        messages.ArmingMode.NIGHT_ARM_WITH_NO_ENTRY_DELAY,
    ):
        return STATUS_ARMED_NIGHT
    return STATUS_ARMED_AWAY


def zone_label_to_name(label: str) -> str:
    """Join a 28-character two-line zone label into a display name."""
    line1 = label[:14].strip()
    line2 = label[14:28].strip()
    return " ".join(part for part in (line1, line2) if part)


@dataclass
class SessionState:
    """Snapshot state of one connected panel."""

    session_id: str
    capabilities: SystemCapabilities = field(default_factory=SystemCapabilities)
    zones: dict[int, ZoneState] = field(default_factory=dict)
    partitions: dict[int, PartitionState] = field(default_factory=dict)


class PanelStateStore:
    """Tracks state for one session and emits per-entity change callbacks."""

    def __init__(self, session_id: str) -> None:
        self.state = SessionState(session_id)
        self.on_partition_change: Callable[[PartitionState], None] | None = None
        self.on_zone_change: Callable[[ZoneState], None] | None = None

    # --- lookup helpers ---
    def partition(self, number: int) -> PartitionState:
        """Get or create a partition."""
        return self.state.partitions.setdefault(number, PartitionState(number))

    def zone(self, number: int) -> ZoneState:
        """Get or create a zone."""
        return self.state.zones.setdefault(number, ZoneState(number))

    def active_zones(self) -> list[ZoneState]:
        """Zones that warrant an entity.

        Without installer-level configuration data, "programmed" zones are
        heuristically the ones with a label or any non-secure status bit.
        """
        return [
            zone
            for zone in self.state.zones.values()
            if zone.name
            or zone.is_open
            or zone.is_bypassed
            or zone.is_faulted
            or zone.is_tampered
        ]

    def _changed_partition(self, partition: PartitionState) -> None:
        if self.on_partition_change is not None:
            self.on_partition_change(partition)

    def _changed_zone(self, zone: ZoneState) -> None:
        if self.on_zone_change is not None:
            self.on_zone_change(zone)

    # --- notification entry point ---
    def apply(self, cmd: int, data: bytes) -> None:
        """Apply an inbound notification packet."""
        notification = self._decode_notification(cmd, data)
        if notification is None:
            return
        self._apply_notification(notification)

    def _apply_notification(self, notification: messages.Notification) -> None:
        match notification:
            case LifestyleZoneNotification():
                self._apply_lifestyle_zone(notification)
            case ArmDisarmNotification():
                self._apply_arm_disarm(notification)
            case ExitDelayNotification():
                self._apply_exit_delay(notification)
            case ReadyStatusNotification():
                self._apply_ready_status(notification)
            case BypassStatusNotification():
                self._apply_bypass_status(notification)
            case ChimeNotification():
                self._apply_chime(notification)
            case LabelTextResponse():
                self.apply_label_response(notification)
            case ZoneStatusResponse():
                self.apply_zone_status_response(notification)
            case PartitionStatusResponse():
                self.apply_partition_status_response(notification)

    @staticmethod
    def _decode_notification(cmd: int, data: bytes) -> messages.Notification | None:
        """Decode a notification payload, logging malformed input."""
        try:
            notification = messages.parse_notification(cmd, data)
        except ValueError, IndexError:
            _LOGGER.warning(
                "Malformed notification cmd=0x%04X (%s bytes) - ignored",
                cmd,
                len(data),
            )
            return None
        if notification is None and cmd == messages.CMD_COMMAND_ERROR:
            error = messages.decode_command_error(data)
            _LOGGER.debug(
                "Panel reported command error for 0x%04X code 0x%02X",
                error.nack_command,
                error.nack_code,
            )
        return notification

    # --- individual notifications ---
    def _apply_lifestyle_zone(self, notification: LifestyleZoneNotification) -> None:
        zone = self.zone(notification.zone)
        zone.is_open = notification.is_open
        if not zone.partitions:
            zone.partitions = [1]
        _LOGGER.debug(
            "Zone %s is now %s", zone.number, "OPEN" if zone.is_open else "CLOSED"
        )
        self._changed_zone(zone)

    def _apply_arm_disarm(self, notification: ArmDisarmNotification) -> None:
        partition = self.partition(notification.partition)
        partition.status = status_from_arming_mode(notification.arm_mode)
        if notification.arm_mode == messages.ArmingMode.DISARM:
            self._clear_exit_delay(partition)
        _LOGGER.info(
            "Partition %s: %s (method %s, user %s)",
            partition.number,
            partition.status,
            notification.method,
            notification.user_id,
        )
        self._changed_partition(partition)

    def _apply_exit_delay(self, notification: ExitDelayNotification) -> None:
        partition = self.partition(notification.partition)
        if notification.is_active and notification.duration_seconds > 0:
            partition.exit_delay_active = True
            partition.exit_delay_seconds = notification.duration_seconds
            partition.exit_delay_audible = notification.is_audible
            partition.status = STATUS_ARMING
        else:
            # Delay expired or was cancelled; the armed/disarmed truth
            # arrives separately via an arm or ready notification, but an
            # arming state without an active delay means we are not arming.
            if partition.status == STATUS_ARMING:
                partition.status = STATUS_DISARMED
            self._clear_exit_delay(partition)
        _LOGGER.info(
            "Partition %s: exit delay %s (%ss)",
            partition.number,
            "active" if partition.exit_delay_active else "finished",
            notification.duration_seconds,
        )
        self._changed_partition(partition)

    def _apply_ready_status(self, notification: ReadyStatusNotification) -> None:
        partition = self.partition(notification.partition)
        # Any ready-status notification means the partition is disarmed; this
        # clears stale armed/arming state if the panel cancelled the arming.
        partition.status = STATUS_DISARMED
        partition.is_ready = notification.ready_status in (
            ReadyStatus.READY_TO_ARM,
            ReadyStatus.READY_TO_FORCE_ARM,
        )
        self._clear_exit_delay(partition)
        _LOGGER.debug(
            "Partition %s ready status 0x%02X",
            partition.number,
            notification.ready_status,
        )
        self._changed_partition(partition)

    def _apply_bypass_status(self, notification: BypassStatusNotification) -> None:
        zone = self.zone(notification.zone)
        zone.is_bypassed = notification.is_bypassed
        _LOGGER.debug("Zone %s bypass status: %s", zone.number, zone.is_bypassed)
        self._changed_zone(zone)

    def _apply_chime(self, notification: ChimeNotification) -> None:
        partition = self.partition(notification.partition)
        partition.chime_enabled = notification.enabled
        _LOGGER.debug(
            "Partition %s door chime: %s",
            partition.number,
            "on" if notification.enabled else "off",
        )
        self._changed_partition(partition)

    @staticmethod
    def _clear_exit_delay(partition: PartitionState) -> None:
        partition.exit_delay_active = False
        partition.exit_delay_seconds = 0
        partition.exit_delay_audible = False

    # --- bulk / response application ---
    def apply_label_response(self, response: LabelTextResponse) -> None:
        """Apply zone or partition labels from a label-text message.

        Empty labels are ignored so that placeholder ranges (e.g. label
        requests covering disabled partitions) do not create state entries.
        """
        if response.collection == LabelCollection.ZONE:
            for index, label in enumerate(response.labels):
                if name := zone_label_to_name(label):
                    zone = self.zone(response.start + index)
                    zone.name = name
                    self._changed_zone(zone)
        elif response.collection == LabelCollection.PARTITION:
            for index, label in enumerate(response.labels):
                if name := label.strip():
                    partition = self.partition(response.start + index)
                    partition.name = name
                    self._changed_partition(partition)

    def apply_partition_status_response(
        self, response: PartitionStatusResponse
    ) -> None:
        partition = self.partition(response.partition)
        partition.status = status_from_flags(response.status1, response.status2)
        partition.is_ready = not (
            response.status1 & messages.PartitionStatus1Flag.ARMED
        ) and bool(
            response.status1
            & (
                messages.PartitionStatus1Flag.READY_TO_ARM
                | messages.PartitionStatus1Flag.READY_TO_FORCE_ARM
            )
        )
        partition.exit_delay_active = bool(
            response.status1 & messages.PartitionStatus1Flag.EXIT_DELAY_IN_PROGRESS
        )
        partition.chime_enabled = bool(
            response.status2 & PartitionStatus2Flag.DOOR_CHIME_ENABLED
        )
        _LOGGER.debug(
            "Partition %s status1=0x%02X status2=0x%02X -> %s",
            partition.number,
            response.status1,
            response.status2,
            partition.status,
        )
        self._changed_partition(partition)

    def apply_zone_status_response(self, response: ZoneStatusResponse) -> None:
        for index, status_byte in enumerate(response.statuses):
            zone = self.zone(response.zone_start + index)
            zone.is_open = bool(status_byte & ZoneStatusFlag.OPEN)
            zone.is_faulted = bool(status_byte & ZoneStatusFlag.FAULT)
            zone.is_tampered = bool(status_byte & ZoneStatusFlag.TAMPER)
            zone.is_bypassed = bool(status_byte & ZoneStatusFlag.BYPASS)
            if not zone.partitions:
                zone.partitions = [1]
            self._changed_zone(zone)

    def snapshot(self) -> SessionState:
        """Return a copy of the current state (safe to hand to callers)."""
        return replace(
            self.state,
            zones={n: replace(z) for n, z in self.state.zones.items()},
            partitions={n: replace(p) for n, p in self.state.partitions.items()},
        )
