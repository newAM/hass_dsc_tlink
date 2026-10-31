"""ITv2 message set used by the integration.

Only the subset of the ~40 ITv2 messages needed for monitoring and control
is implemented (see the protocol documentation in the NeoHub repository
for the full catalog):

* connection management: OpenSession, RequestAccess, CommandResponse, poll
* queries: system capabilities, zone/partition status, labels
* commands: partition arm/disarm, single-zone bypass (with config enter/exit)
* notifications: zone open/close, arm/disarm, exit delay, ready status,
  bypass status, label text

Encoding follows the C# ``BinarySerializer`` attribute semantics:
``[CompactInteger]`` → :func:`tlink.framing.compact_int`, ``[BCDString]`` →
high-nibble-first BCD right-padded with zeros, labels → UTF-16BE
fixed-length arrays with a compact-integer byte width.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from .framing import compact_int, parse_compact_int, u16

# --- ITv2 command words (big-endian u16 message type) -------------------------
CMD_COMMAND_ERROR = 0x0501
CMD_COMMAND_RESPONSE = 0x0502
CMD_CONNECTION_POLL = 0x0600
CMD_OPEN_SESSION = 0x060A
CMD_END_SESSION = 0x060B
CMD_REQUEST_ACCESS = 0x060E
CMD_MULTIPLE_MESSAGE_PACKET = 0x0623
CMD_SYSTEM_CAPABILITIES = 0x0613
CMD_CONFIGURATION_EXIT = 0x0701
CMD_CONFIGURATION_ENTER = 0x0704
CMD_SINGLE_ZONE_BYPASS_WRITE = 0x074A
CMD_NOTIFICATION_LABEL_TEXT = 0x0771
CMD_COMMAND_REQUEST = 0x0800
CMD_ZONE_STATUS = 0x0811
CMD_PARTITION_STATUS = 0x0812
CMD_SINGLE_ZONE_BYPASS_STATUS = 0x0820
CMD_PARTITION_ARM = 0x0900
CMD_PARTITION_DISARM = 0x0901
CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS = 0x0210
CMD_NOTIFICATION_CHIME = 0x0223
CMD_NOTIFICATION_EXIT_DELAY = 0x0230
CMD_NOTIFICATION_ARM_DISARM = 0x0232
CMD_NOTIFICATION_PARTITION_READY_STATUS = 0x0239


# --- Enums ---------------------------------------------------------------------
class ArmingMode:
    """``ArmingMode`` wire values sent with :code:`0x0900`."""

    DISARM = 0x00
    STAY_ARM = 0x01
    AWAY_ARM = 0x02
    ARM_WITH_NO_ENTRY_DELAY = 0x03
    NIGHT_ARM = 0x04
    STAY_ARM_WITH_NO_ENTRY_DELAY = 0x07
    AWAY_ARM_WITH_NO_ENTRY_DELAY = 0x08
    NIGHT_ARM_WITH_NO_ENTRY_DELAY = 0x09


class ZoneStatusFlag:
    """Zone status byte bits (``ModuleZoneStatus.ZoneStatusEnum``)."""

    OPEN = 0x01
    TAMPER = 0x02
    FAULT = 0x04
    LOW_BATTERY = 0x08
    DELINQUENCY = 0x10
    ALARM = 0x20
    ALARM_MEMORY = 0x40
    BYPASS = 0x80


class PartitionStatus1Flag:
    """First partition status byte (a combined flags/state byte)."""

    ARMED = 0x01
    STAY_ARMED = 0x03  # ARMED | READY_TO_ARM
    READY_TO_ARM = 0x02
    READY_TO_FORCE_ARM = 0x04
    AWAY_ARMED = 0x05  # ARMED | READY_TO_FORCE_ARM
    NOT_READY = 0x08
    NIGHT_ARMED = 0x09  # ARMED | NOT_READY
    WALK_TEST_MODE = 0x10
    ARMED_WITH_NO_ENTRY_DELAY = 0x11
    EXIT_DELAY_IN_PROGRESS = 0x20
    ENTRY_DELAY_IN_PROGRESS = 0x40
    QUICK_EXIT_IN_PROGRESS = 0x80


class PartitionStatus2Flag:
    """Second partition status byte."""

    PARTITION_IN_ALARM = 0x01
    TROUBLES_PRESENT = 0x02
    ZONES_BYPASSED = 0x04
    IN_PROGRAMMING_MODE = 0x08
    ALARMS_IN_MEMORY = 0x10
    DOOR_CHIME_ENABLED = 0x20
    AUDIBLE_BELL_SIREN = 0x40
    KEYPAD_BUZZER_ALARM = 0x80


class ExitDelayFlag:
    AUDIBLE = 0x01
    HAS_RESTARTED = 0x02
    URGENT = 0x04
    ACTIVE = 0x80


class ReadyStatus:
    """``NotificationPartitionReadyStatus`` status byte."""

    RESERVED = 0x00
    READY_TO_ARM = 0x01
    READY_TO_FORCE_ARM = 0x02
    NOT_READY_TO_ARM = 0x03


class LifestyleZoneStatus:
    RESTORED = 0x00
    OPEN = 0x01


class LabelCollection:
    ZONE = 0xD1
    PARTITION = 0xD3
    USER = 0xD9


class ProgrammingMode:
    USER_BYPASS_PROGRAMMING = 0x03


class ResponseCode:
    """Panel ``CommandResponse`` codes (subset with descriptions)."""

    DESCRIPTIONS: ClassVar[dict[int, str]] = {
        0x00: "Success",
        0x01: "Cannot exit configuration / block out of range",
        0x02: "Invalid programming type",
        0x03: "Unsupported module",
        0x04: "Invalid signal type / some or all partitions failed to arm",
        0x10: "Not in correct programming mode",
        0x11: "Invalid access code",
        0x12: "Access code is required",
        0x13: "System or partition is busy",
        0x14: "Invalid partition",
        0x17: "Function not available",
        0x18: "Internal error",
        0x19: "Command timeout",
        0x1A: "No troubles present for requested type",
        0x1B: "No requested alarms found",
        0x1C: "Invalid device module",
        0x1D: "Invalid trouble type",
    }

    @classmethod
    def describe(cls, code: int) -> str:
        return cls.DESCRIPTIONS.get(code, f"Unknown response code 0x{code:02X}")


# --- BCD / digit helpers -------------------------------------------------------
def bcd_encode(digits: str) -> bytes:
    """BCD-encode a digit (or hex sentinel) string, zero right-padded.

    An empty string encodes to zero bytes (matching the C# serializer's
    unbounded BCD behavior used for codeless arming).
    """
    if not digits:
        return b""
    if any(c not in "0123456789abcdefABCDEF" for c in digits):
        msg = f"invalid BCD string: {digits!r}"
        raise ValueError(msg)
    padded = digits + "0" * (-len(digits) % 2)
    return bytes(
        (int(padded[i], 16) << 4) | int(padded[i + 1], 16)
        for i in range(0, len(padded), 2)
    )


def bcd_decode(data: bytes) -> str:
    """Decode BCD bytes to a digit string, trailing zeros trimmed."""
    digits = "".join(f"{byte >> 4:X}{byte & 0x0F:X}" for byte in data)
    return digits.rstrip("0")


def digit_bytes_encode(digits: str) -> bytes:
    """Length-prefixed raw digit bytes (one byte per digit, value 0-9).

    Used by ``ConfigurationEnter`` - the panel rejects BCD or ASCII here.
    """
    if not digits.isdigit():
        msg = f"access code must be digits only: {digits!r}"
        raise ValueError(msg)
    return bytes((len(digits),)) + bytes(int(c) for c in digits)


def _utf16be_fixed_array_encode(strings: list[str]) -> bytes:
    """[CompactInt byte width][UTF-16BE strings zero-padded to width]."""
    width = max((len(s.encode("utf-16-be")) for s in strings), default=0)
    out = bytearray(compact_int(width))
    for string in strings:
        encoded = string.encode("utf-16-be")
        out += encoded + b"\x00" * (width - len(encoded))
    return bytes(out)


def _utf16be_fixed_array_decode(data: bytes, offset: int) -> tuple[list[str], int]:
    width, offset = parse_compact_int(data, offset)
    labels: list[str] = []
    while offset < len(data):
        if offset + width > len(data):
            msg = "truncated fixed-length unicode label"
            raise ValueError(msg)
        raw = data[offset : offset + width]
        offset += width
        labels.append(raw.decode("utf-16-be").rstrip("\x00"))
    return labels, offset


# --- Decoded message dataclasses ------------------------------------------------
@dataclass(frozen=True)
class SystemCapabilities:
    max_zones: int = 0
    max_users: int = 0
    max_partitions: int = 0
    max_fobs: int = 0
    max_prox_tags: int = 0
    max_outputs: int = 0


@dataclass(frozen=True)
class ZoneStatusResponse:
    zone_start: int
    statuses: list[int]


@dataclass(frozen=True)
class PartitionStatusResponse:
    partition: int
    status1: int
    status2: int


@dataclass(frozen=True)
class LabelTextResponse:
    collection: int
    start: int
    labels: list[str]


@dataclass(frozen=True)
class LifestyleZoneNotification:
    zone: int
    is_open: bool


@dataclass(frozen=True)
class ArmDisarmNotification:
    partition: int
    arm_mode: int
    method: int
    user_id: int


@dataclass(frozen=True)
class ExitDelayNotification:
    partition: int
    flags: int
    duration_seconds: int

    @property
    def is_active(self) -> bool:
        return bool(self.flags & ExitDelayFlag.ACTIVE)

    @property
    def is_audible(self) -> bool:
        return bool(self.flags & ExitDelayFlag.AUDIBLE)


@dataclass(frozen=True)
class ReadyStatusNotification:
    partition: int
    ready_status: int


@dataclass(frozen=True)
class BypassStatusNotification:
    zone: int
    is_bypassed: bool


@dataclass(frozen=True)
class ChimeNotification:
    partition: int
    enabled: bool


@dataclass(frozen=True)
class CommandErrorResponse:
    nack_command: int
    nack_code: int


Notification = (
    LifestyleZoneNotification
    | ArmDisarmNotification
    | ExitDelayNotification
    | ReadyStatusNotification
    | BypassStatusNotification
    | ChimeNotification
    | LabelTextResponse
    | ZoneStatusResponse
    | PartitionStatusResponse
)


# --- Decoders -------------------------------------------------------------------
def decode_capabilities(data: bytes) -> SystemCapabilities:
    fields = []
    offset = 0
    for _ in range(6):
        value, offset = parse_compact_int(data, offset)
        fields.append(value)
    return SystemCapabilities(*fields)


def decode_zone_status(data: bytes) -> ZoneStatusResponse:
    zone_start, offset = parse_compact_int(data, 0)
    _, offset = parse_compact_int(data, offset)
    offset += 1  # status size in bytes (always 1)
    return ZoneStatusResponse(zone_start, list(data[offset:]))


def decode_partition_status(data: bytes) -> PartitionStatusResponse:
    partition, offset = parse_compact_int(data, 0)
    length = data[offset]
    offset += 1
    statuses = data[offset : offset + length]
    status1 = statuses[0] if len(statuses) > 0 else 0
    status2 = statuses[1] if len(statuses) > 1 else 0
    return PartitionStatusResponse(partition, status1, status2)


def decode_label_text(data: bytes) -> LabelTextResponse:
    collection, offset = parse_compact_int(data, 0)
    start, offset = parse_compact_int(data, offset)
    _end, offset = parse_compact_int(data, offset)
    labels, _ = _utf16be_fixed_array_decode(data, offset)
    return LabelTextResponse(collection, start, labels)


def decode_lifestyle_zone(data: bytes) -> LifestyleZoneNotification:
    zone, offset = parse_compact_int(data, 0)
    return LifestyleZoneNotification(zone, data[offset] == LifestyleZoneStatus.OPEN)


def decode_arm_disarm(data: bytes) -> ArmDisarmNotification:
    partition, offset = parse_compact_int(data, 0)
    arm_mode = data[offset]
    method = data[offset + 1]
    user_id, _ = parse_compact_int(data, offset + 2)
    return ArmDisarmNotification(partition, arm_mode, method, user_id)


def decode_exit_delay(data: bytes) -> ExitDelayNotification:
    partition, offset = parse_compact_int(data, 0)
    flags = data[offset]
    duration, _ = parse_compact_int(data, offset + 1)
    return ExitDelayNotification(partition, flags, duration)


def decode_ready_status(data: bytes) -> ReadyStatusNotification:
    partition, offset = parse_compact_int(data, 0)
    return ReadyStatusNotification(partition, data[offset])


def decode_bypass_status(data: bytes) -> BypassStatusNotification:
    zone, offset = parse_compact_int(data, 0)
    return BypassStatusNotification(zone, data[offset] != 0)


def decode_chime(data: bytes) -> ChimeNotification:
    """Decode ``NotificationChimeBroadcast`` (0x0223).

    Format is inferred from the sibling partition notifications:
    ``[CompactInt partition][enabled byte]``.
    """
    partition, offset = parse_compact_int(data, 0)
    return ChimeNotification(partition, data[offset] != 0)


def decode_command_error(data: bytes) -> CommandErrorResponse:
    return CommandErrorResponse((data[0] << 8) | data[1], data[2])


def parse_notification(cmd: int, data: bytes) -> Notification | None:  # noqa: PLR0911
    """Decode an inbound packet payload by command word.

    Returns ``None`` for command words we do not interpret (they are still
    acked at the session layer).
    """
    match cmd:
        case c if c == CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS:
            return decode_lifestyle_zone(data)
        case c if c == CMD_NOTIFICATION_ARM_DISARM:
            return decode_arm_disarm(data)
        case c if c == CMD_NOTIFICATION_EXIT_DELAY:
            return decode_exit_delay(data)
        case c if c == CMD_NOTIFICATION_PARTITION_READY_STATUS:
            return decode_ready_status(data)
        case c if c == CMD_SINGLE_ZONE_BYPASS_STATUS:
            return decode_bypass_status(data)
        case c if c == CMD_NOTIFICATION_CHIME:
            return decode_chime(data)
        case c if c == CMD_NOTIFICATION_LABEL_TEXT:
            return decode_label_text(data)
        case c if c == CMD_ZONE_STATUS:
            # Standalone (unsolicited) zone status pushes; query responses
            # are consumed by the pending waiter and never reach here.
            return decode_zone_status(data)
        case c if c == CMD_PARTITION_STATUS:
            return decode_partition_status(data)
        case _:
            return None


# --- Encoders (embedded payloads wrapped in a 0x0800 CommandRequest) -------------
def encode_capabilities_request() -> bytes:
    """``ConnectionSystemCapabilities`` request: six zero compact integers."""
    return compact_int(0) * 6


def encode_zone_status_request(start: int, count: int) -> bytes:
    """``ModuleZoneStatus`` query for zones [start, start+count)."""
    return compact_int(start) + compact_int(count) + b"\x00"


def encode_partition_status_request(partition: int) -> bytes:
    """``ModulePartitionStatus`` query with an empty status array."""
    return compact_int(partition) + b"\x00"


def encode_label_request(collection: int, start: int, end: int) -> bytes:
    """``NotificationLabelText`` request for labels [start, end]."""
    return compact_int(collection) + compact_int(start) + compact_int(end)


def encode_arm(partition: int, arm_mode: int, access_code: str = "") -> bytes:
    """``PartitionArm`` (0x0900): [CompactInt partition][mode][BCD code]."""
    return compact_int(partition) + bytes((arm_mode,)) + bcd_encode(access_code)


def encode_disarm(partition: int, access_code: str) -> bytes:
    """``PartitionDisarm`` (0x0901): [CompactInt partition][BCD code]."""
    return compact_int(partition) + bcd_encode(access_code)


def encode_config_enter(
    partition: int,
    access_code: str,
    programming_mode: int = ProgrammingMode.USER_BYPASS_PROGRAMMING,
) -> bytes:
    """``ConfigurationEnter`` (0x0704) with a raw-digit access code."""
    return (
        compact_int(partition)
        + bytes((programming_mode,))
        + digit_bytes_encode(access_code)
        + b"\x01"  # read/write mode
    )


def encode_config_exit(partition: int) -> bytes:
    """``ConfigurationExit`` (0x0701)."""
    return compact_int(partition)


def encode_bypass_write(partition: int, zone: int, bypass: bool) -> bytes:
    """``SingleZoneBypassWrite`` (0x074A)."""
    return compact_int(partition) + compact_int(zone) + bytes((int(bypass),))


def encode_multiple_messages(entries: list[tuple[int, bytes]]) -> bytes:
    """Build a ``MultipleMessagePacket`` (0x0623) payload.

    Each sub-message is ``[length][u16 command word][message data]``,
    with the length prefix in the ITv2 framing encoding (one byte when
    < 0x80, otherwise two bytes with bit 7 set on the first).
    """
    out = bytearray()
    for cmd, payload in entries:
        body = u16(cmd) + payload
        length = len(body)
        if length > 0x7F:
            out += bytes(((length >> 8) | 0x80, length & 0xFF))
        else:
            out += bytes((length,))
        out += body
    return bytes(out)


def decode_multiple_messages(data: bytes) -> list[tuple[int, bytes]]:
    """Unwrap a ``MultipleMessagePacket`` (0x0623) into (cmd, payload) pairs.

    Raises ``ValueError`` on truncated or command-less sub-messages.
    """
    entries: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        length = data[offset]
        offset += 1
        if length & 0x80:
            length = ((length & 0x7F) << 8) | data[offset]
            offset += 1
        body = data[offset : offset + length]
        if len(body) < length:
            msg = "truncated sub-message in MultipleMessagePacket"
            raise ValueError(msg)
        offset += length
        if len(body) < 2:
            msg = "sub-message without a command word"
            raise ValueError(msg)
        entries.append((((body[0] << 8) | body[1]), body[2:]))
    return entries


# --- Full-packet body helpers ----------------------------------------------------
def command_request_body(command_seq: int, embedded_cmd: int, payload: bytes) -> bytes:
    """Body of a ``CommandRequestMessage`` (0x0800) packet."""
    return (
        u16(CMD_COMMAND_REQUEST) + bytes((command_seq,)) + u16(embedded_cmd) + payload
    )


def response_body(command_seq: int, code: int) -> bytes:
    """Body of a ``CommandResponse`` (0x0502) packet."""
    return u16(CMD_COMMAND_RESPONSE) + bytes((command_seq, code))


def poll_body() -> bytes:
    """Body of a ``ConnectionPoll`` (0x0600) packet."""
    return u16(CMD_CONNECTION_POLL)


def end_session_body() -> bytes:
    """Body of a ``ConnectionEndSession`` (0x060B) packet."""
    return u16(CMD_END_SESSION)


@dataclass
class Packet:
    """A parsed ITv2 packet (post-framing, pre-encryption)."""

    sender_seq: int
    receiver_seq: int
    cmd: int  # 0 means SimpleAck (no command word)
    data: bytes  # message data after the command word
    is_ack: bool

    command_response: CommandErrorResponse | None = field(default=None, init=False)

    @classmethod
    def from_message(cls, message: bytes) -> Packet:
        """Parse a framed message body into sender/receiver/cmd/data."""
        sender_seq = message[0]
        receiver_seq = message[1]
        rest = message[2:]
        if len(rest) >= 2:
            cmd = (rest[0] << 8) | rest[1]
            return cls(sender_seq, receiver_seq, cmd, rest[2:], False)
        return cls(sender_seq, receiver_seq, 0, b"", True)
