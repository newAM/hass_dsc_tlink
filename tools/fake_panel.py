#!/usr/bin/env python3
"""A fake DSC PowerSeries NEO panel for developing and testing the server.

The real panel is always the TLink *client*: it connects to the hub and
initiates the OpenSession/RequestAccess handshake. This tool plays that
role against the ``dsc_tlink`` server (or the Home Assistant integration),
with a small canned configuration:

* 8 zones (labels for all of them)
* partition 1 ("Home") - partitions 2-8 respond with InvalidPartition,
  exercising the server's disabled-partition handling
* arm/disarm/bypass commands update internal state and emit the same
  notifications a real panel sends

Usage:
    python tools/fake_panel.py --host 127.0.0.1 --port 3072

    # then flip a zone open/closed every 10 seconds:
    python tools/fake_panel.py --toggle-zone 1
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "dsc_tlink")
)

from tlink import (
    DEFAULT_TYPE2_ACCESS_CODE,
    Type1Encryption,
    Type2Encryption,
    messages,
)
from tlink.crypto import Encryption
from tlink.framing import (
    TLinkDecoder,
    add_framing,
    decode_tlink,
    encode_tlink,
    remove_framing,
    u16,
)
from tlink.messages import Packet

_LOGGER = logging.getLogger("fake_panel")

ZONE_LABELS = {
    1: "Front Door",
    2: "Back Door",
    3: "Kitchen Window",
    4: "Living Room Motion",
    5: "Master Bedroom Window",
    6: "Hallway Motion",
    7: "Garage Door",
    8: "Hallway Smoke Detector",
}
PARTITION_LABELS = {1: "Home"}
MAX_ZONES = 8
MAX_PARTITIONS = 8
MAX_USERS = 95

# Partition status1 values while disarmed / armed.
STATUS1_DISARMED_READY = 0x02
STATUS1_DISARMED_NOT_READY = 0x08
STATUS1_ARMED_AWAY = 0x05
STATUS1_ARMED_STAY = 0x03
STATUS1_ARMED_NIGHT = 0x09


class FakePanel:
    """A scripted DSC panel that connects to a TLink server."""

    def __init__(  # noqa: PLR0913 - scripted-panel configuration surface
        self,
        host: str,
        port: int,
        *,
        access_code: str = DEFAULT_TYPE2_ACCESS_CODE,
        encryption_type: int = 2,
        integration_id_header: bytes = b"0001234501",
        integration_id_code: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.access_code = access_code
        self.encryption_type = encryption_type
        self.integration_id_header = integration_id_header
        self.integration_id_code = integration_id_code
        self.open_zones: set[int] = set()
        self.bypassed_zones: set[int] = set()
        self.alarmed_zones: set[int] = set()
        self.arm_modes: dict[int, int] = {1: messages.ArmingMode.DISARM}
        # Reflected in partition status2 (DoorChimeEnabled) and chime
        # broadcasts; this firmware rejects remote chime writes.
        self.chime_enabled: bool = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = TLinkDecoder()
        self._pending: list[bytes] = []
        self._encryption: Encryption | None = None
        self._local_seq = 1
        self._remote_seq = 0
        self._command_seq = 0
        self._encrypt_out = False
        self._decrypt_in = False
        self.disconnected = asyncio.Event()

    # --- low level ---
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.encryption_type == 1:
            self._encryption = Type1Encryption(
                self.access_code,
                self.integration_id_code or "11111111",
                panel_role=True,
            )
        else:
            self._encryption = Type2Encryption(self.access_code)

    async def _recv_packet(self) -> Packet:
        assert self._reader is not None
        while True:
            if self._pending:
                raw = self._pending.pop(0)
                break
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self._pending = list(self._decoder.feed(chunk))
        _, payload = decode_tlink(raw)
        if self._decrypt_in:
            payload = self._encryption.decrypt(payload)
        packet = Packet.from_message(remove_framing(payload))
        self._remote_seq = packet.sender_seq
        return packet

    async def _send_packet(
        self, body: bytes, *, receiver_seq: int | None = None
    ) -> None:
        assert self._writer is not None
        receiver = self._remote_seq if receiver_seq is None else receiver_seq
        plaintext = add_framing(bytes((self._local_seq, receiver)) + body)
        if self._encrypt_out:
            plaintext = self._encryption.encrypt(plaintext)
        self._writer.write(encode_tlink(self.integration_id_header, plaintext))
        await self._writer.drain()
        self._local_seq = (self._local_seq + 1) & 0xFF

    async def _ack(self, sender_seq: int) -> None:
        await self._send_packet(b"", receiver_seq=sender_seq)

    # --- handshake (mirror of PanelSession._handshake) ---
    async def handshake(self) -> None:
        self._command_seq = 1
        # 1. OpenSession (plaintext): [cmdSeq][13 filler bytes][encType]
        open_data = (
            bytes((self._command_seq,))
            + b"FAKEPANEL00000"[:13]
            + bytes((self.encryption_type,))
        )
        await self._send_packet(u16(messages.CMD_OPEN_SESSION) + open_data)

        # 2. Receive hub CommandResponse, 3. SimpleAck.
        packet = await self._recv_packet()
        assert packet.cmd == messages.CMD_COMMAND_RESPONSE
        await self._ack(packet.sender_seq)

        # 4. Receive echoed OpenSession, send CommandResponse, await ack.
        echo = await self._recv_packet()
        assert echo.cmd == messages.CMD_OPEN_SESSION
        echo_cmd_seq = echo.data[0]
        await self._send_packet(
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((echo_cmd_seq, 0x00))
        )
        await self._recv_packet()  # SimpleAck

        # 5. RequestAccess with our initializer (plaintext).
        self._command_seq = 2
        initializer = self._encryption.configure_inbound()
        await self._send_packet(
            u16(messages.CMD_REQUEST_ACCESS)
            + bytes((self._command_seq, len(initializer)))
            + initializer
        )

        # 6. Hub CommandResponse is now ENCRYPTED with the key derived from
        #    OUR initializer (= the hub's outbound key).
        self._decrypt_in = True
        packet = await self._recv_packet()
        assert packet.cmd == messages.CMD_COMMAND_RESPONSE
        await self._ack(packet.sender_seq)

        # 7. Hub RequestAccess (encrypted); its initializer seeds OUR
        #    outbound key. We answer encrypted from here on.
        request_access = await self._recv_packet()
        assert request_access.cmd == messages.CMD_REQUEST_ACCESS
        init_len = request_access.data[1]
        self._encryption.configure_outbound(request_access.data[2 : 2 + init_len])
        self._encrypt_out = True
        self._command_seq = 3
        await self._send_packet(
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((request_access.data[0], 0x00))
        )
        await self._recv_packet()  # SimpleAck (encrypted)
        _LOGGER.info("Handshake complete")

    # --- main loop ---
    async def run(self) -> None:
        try:
            while True:
                packet = await self._recv_packet()
                if packet.is_ack:
                    continue
                if packet.cmd == messages.CMD_END_SESSION:
                    await self._ack(packet.sender_seq)
                    break
                if packet.cmd == messages.CMD_COMMAND_REQUEST:
                    await self._handle_command_request(packet)
                    continue
                await self._ack(packet.sender_seq)
        except ConnectionError, OSError:
            pass
        finally:
            self.disconnected.set()

    async def _handle_command_request(self, packet: Packet) -> None:
        await self._ack(packet.sender_seq)
        request_seq = packet.data[0]
        embedded_cmd = (packet.data[1] << 8) | packet.data[2]
        payload = packet.data[3:]

        handlers = {
            messages.CMD_SYSTEM_CAPABILITIES: self._handle_capabilities,
            messages.CMD_NOTIFICATION_LABEL_TEXT: self._handle_labels,
            messages.CMD_PARTITION_STATUS: self._handle_partition_status,
            messages.CMD_ZONE_STATUS: self._handle_zone_status,
            messages.CMD_PARTITION_ARM: self._handle_arm,
            messages.CMD_PARTITION_DISARM: self._handle_disarm,
            messages.CMD_CONFIGURATION_ENTER: self._handle_config_enter,
            messages.CMD_CONFIGURATION_EXIT: self._handle_config_exit,
            0x074A: self._handle_bypass_write,
        }
        handler = handlers.get(embedded_cmd)
        if handler is None:
            await self._send_packet(
                u16(messages.CMD_COMMAND_RESPONSE)
                + bytes((request_seq, 0x17))  # Function not available
            )
            return
        await handler(request_seq, payload)

    # --- embedded command handlers ---
    async def _respond(self, cmd: int, data: bytes) -> None:
        await self._send_packet(u16(cmd) + data)

    async def _respond_ok(self, request_seq: int) -> None:
        await self._send_packet(
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((request_seq, 0x00))
        )

    async def _handle_capabilities(self, request_seq: int, payload: bytes) -> None:
        del payload
        await self._respond(
            messages.CMD_SYSTEM_CAPABILITIES,
            messages.compact_int(MAX_ZONES)
            + messages.compact_int(MAX_USERS)
            + messages.compact_int(MAX_PARTITIONS)
            + messages.compact_int(0)
            + messages.compact_int(0)
            + messages.compact_int(0),
        )

    async def _handle_labels(self, request_seq: int, payload: bytes) -> None:
        collection, offset = messages.parse_compact_int(payload, 0)
        start, offset = messages.parse_compact_int(payload, offset)
        end, _ = messages.parse_compact_int(payload, offset)
        labels = (
            PARTITION_LABELS
            if collection == messages.LabelCollection.PARTITION
            else ZONE_LABELS
        )
        requested = [labels.get(number, "") for number in range(start, end + 1)]
        width = max((len(s.encode("utf-16-be")) for s in requested), default=0)
        data = (
            messages.compact_int(collection)
            + messages.compact_int(start)
            + messages.compact_int(end)
            + messages.compact_int(width)
        )
        for label in requested:
            encoded = label.encode("utf-16-be")
            data += encoded + b"\x00" * (width - len(encoded))
        await self._respond(messages.CMD_NOTIFICATION_LABEL_TEXT, data)

    async def _handle_partition_status(self, request_seq: int, payload: bytes) -> None:
        partition, _ = messages.parse_compact_int(payload, 0)
        if partition != 1:
            await self._send_packet(
                u16(messages.CMD_COMMAND_RESPONSE)
                + bytes((request_seq, 0x14))  # InvalidPartition
            )
            return
        await self._respond(
            messages.CMD_PARTITION_STATUS,
            messages.compact_int(partition)
            + bytes((2, self._partition_status1(), self._partition_status2())),
        )

    def _partition_status1(self) -> int:
        mode = self.arm_modes.get(1, messages.ArmingMode.DISARM)
        if mode == messages.ArmingMode.AWAY_ARM:
            return STATUS1_ARMED_AWAY
        if mode == messages.ArmingMode.STAY_ARM:
            return STATUS1_ARMED_STAY
        if mode == messages.ArmingMode.NIGHT_ARM:
            return STATUS1_ARMED_NIGHT
        if self.open_zones - self.bypassed_zones:
            return STATUS1_DISARMED_NOT_READY
        return STATUS1_DISARMED_READY

    def _partition_status2(self) -> int:
        status2 = 0
        if self.bypassed_zones:
            status2 |= messages.PartitionStatus2Flag.ZONES_BYPASSED
        if self.alarmed_zones:
            status2 |= messages.PartitionStatus2Flag.PARTITION_IN_ALARM
        if self.chime_enabled:
            status2 |= messages.PartitionStatus2Flag.DOOR_CHIME_ENABLED
        return status2

    async def _handle_zone_status(self, request_seq: int, payload: bytes) -> None:
        start, offset = messages.parse_compact_int(payload, 0)
        count, _ = messages.parse_compact_int(payload, offset)
        statuses = [
            self._zone_status_byte(number) for number in range(start, start + count)
        ]
        await self._respond(
            messages.CMD_ZONE_STATUS,
            messages.compact_int(start)
            + messages.compact_int(count)
            + bytes((1,))
            + bytes(statuses),
        )

    def _zone_status_byte(self, number: int) -> int:
        status = 0
        if number in self.open_zones:
            status |= messages.ZoneStatusFlag.OPEN
        if number in self.bypassed_zones:
            status |= messages.ZoneStatusFlag.BYPASS
        if number in self.alarmed_zones:
            status |= messages.ZoneStatusFlag.ALARM
        return status

    async def _handle_arm(self, request_seq: int, payload: bytes) -> None:
        partition, offset = messages.parse_compact_int(payload, 0)
        if partition != 1:
            await self._reject(request_seq, 0x14)  # InvalidPartition
            return
        mode = payload[offset]
        self.arm_modes[partition] = mode
        await self._respond_ok(request_seq)
        await self._send_notification(
            messages.CMD_NOTIFICATION_ARM_DISARM,
            messages.compact_int(partition)
            + bytes((mode, 1))
            + messages.compact_int(1),
        )

    async def _handle_disarm(self, request_seq: int, payload: bytes) -> None:
        partition, _ = messages.parse_compact_int(payload, 0)
        if partition != 1:
            await self._reject(request_seq, 0x14)  # InvalidPartition
            return
        self.arm_modes[partition] = messages.ArmingMode.DISARM
        await self._respond_ok(request_seq)
        await self._send_notification(
            messages.CMD_NOTIFICATION_ARM_DISARM,
            messages.compact_int(partition)
            + bytes((messages.ArmingMode.DISARM, 1))
            + messages.compact_int(1),
        )

    async def _reject(self, request_seq: int, code: int) -> None:
        await self._send_packet(
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((request_seq, code))
        )

    async def _handle_config_enter(self, request_seq: int, payload: bytes) -> None:
        await self._respond_ok(request_seq)

    async def _handle_config_exit(self, request_seq: int, payload: bytes) -> None:
        await self._respond_ok(request_seq)

    async def _handle_bypass_write(self, request_seq: int, payload: bytes) -> None:
        partition, offset = messages.parse_compact_int(payload, 0)
        zone, offset = messages.parse_compact_int(payload, offset)
        bypass = payload[offset] != 0
        del partition
        if bypass:
            self.bypassed_zones.add(zone)
        else:
            self.bypassed_zones.discard(zone)
        await self._respond_ok(request_seq)
        await self._send_notification(
            messages.CMD_SINGLE_ZONE_BYPASS_STATUS,
            messages.compact_int(zone) + bytes((int(bypass),)),
        )

    # --- spontaneous notifications ---
    async def _send_notification(self, cmd: int, data: bytes) -> None:
        await self._send_packet(u16(cmd) + data)

    async def _send_multiple(self, entries: list[tuple[int, bytes]]) -> None:
        """Send a batched MultipleMessagePacket (0x0623)."""
        await self._send_packet(
            u16(messages.CMD_MULTIPLE_MESSAGE_PACKET)
            + messages.encode_multiple_messages(entries)
        )

    async def set_zone_open(self, zone: int, is_open: bool) -> None:
        if is_open:
            self.open_zones.add(zone)
        else:
            self.open_zones.discard(zone)
        await self._send_notification(
            messages.CMD_NOTIFICATION_LIFESTYLE_ZONE_STATUS,
            messages.compact_int(zone) + bytes((int(is_open),)),
        )

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.wait_closed()


async def _amain(args: argparse.Namespace) -> int:
    panel = FakePanel(
        args.host,
        args.port,
        access_code=args.access_code,
        encryption_type=args.type,
        integration_id_code=args.integration_id,
    )
    await panel.connect()
    await panel.handshake()
    print(
        f"Connected to {args.host}:{args.port}; panel id {panel.integration_id_header!r}"
    )

    run_task = asyncio.create_task(panel.run())
    if args.toggle_zone:
        zone = args.toggle_zone
        state = False
        while not run_task.done():
            await asyncio.sleep(args.interval)
            state = not state
            await panel.set_zone_open(zone, state)
            print(f"Zone {zone} -> {'OPEN' if state else 'CLOSED'}")
    await run_task
    await panel.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3072)
    parser.add_argument("--type", type=int, choices=[1, 2], default=2)
    parser.add_argument("--access-code", default=DEFAULT_TYPE2_ACCESS_CODE)
    parser.add_argument(
        "--integration-id", help="Integration ID [851][422] (type 1 only)"
    )
    parser.add_argument(
        "--toggle-zone", type=int, help="flip this zone open/closed periodically"
    )
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
