"""Asyncio ITv2 session for one connected panel.

The session performs the OpenSession/RequestAccess handshake (the panel is
always the initiator), then runs a receive loop that:

* acks every non-ack packet (protocol requirement),
* resolves pending request waiters,
* forwards notifications to a callback.

Requests are sent as ``CommandRequestMessage`` (0x0800) packets wrapping an
embedded command. Two correlation modes exist:

* ``query()`` - waits for the standalone response carrying the embedded
  command word (e.g. zone status, labels, capabilities).
* ``command()`` - waits for a ``CommandResponse`` (0x0502) with the matching
  command sequence; a non-zero code is raised as :class:`PanelCommandError`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable

from . import messages
from .crypto import Encryption, EncryptionError
from .framing import (
    TLinkDecoder,
    add_framing,
    decode_tlink,
    encode_tlink,
    remove_framing,
    u16,
)
from .messages import Packet

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 15.0
REQUEST_TIMEOUT = 15.0
KEEPALIVE_INTERVAL = 100.0
STREAM_LIMIT = 1_048_576  # 1 MiB, comfortably above any ITv2 frame


class TLinkError(Exception):
    """Base error for the tlink library."""


class HandshakeError(TLinkError):
    """The ITv2 handshake failed (wrong credentials or unexpected flow)."""


class SessionClosedError(TLinkError):
    """The panel connection was closed."""


class PanelTimeoutError(TLinkError):
    """The panel did not answer a request in time."""


class PanelCommandError(TLinkError):
    """The panel rejected a command with a non-zero response code."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(messages.ResponseCode.describe(code))


class _Waiter:
    """A pending request awaiting its response."""

    def __init__(
        self,
        expected_cmd: int | None,
        command_seq: int,
        resolve_on_response: bool,
    ) -> None:
        self.expected_cmd = expected_cmd
        self.command_seq = command_seq
        self.resolve_on_response = resolve_on_response
        self.future: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()


class PanelSession:
    """One panel's ITv2 session over an asyncio stream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        encryption: Encryption,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._encryption = encryption
        self._request_timeout = request_timeout
        self._decoder = TLinkDecoder()
        self._pending: deque[bytes] = deque()
        self.header = b""
        self.local_seq = 1
        self.remote_seq = 0
        self.command_seq = 0
        self.encrypt_outbound = False
        self.decrypt_inbound = False
        self.on_notification: Callable[[int, bytes], None] | None = None
        self._waiter: _Waiter | None = None
        self._request_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._handshake_done = False
        self._closing = False

    @property
    def integration_id(self) -> str:
        """The panel's Integration ID, echoed from the TLink header."""
        return self.header.decode("ascii", "replace").strip("\x00 ").strip()

    # --- low-level I/O ---
    async def _next_raw(self) -> bytes:
        """Return the next complete raw TLink packet from the stream."""
        if self._pending:
            return self._pending.popleft()
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                raise SessionClosedError("panel closed the connection")
            packets = list(self._decoder.feed(chunk))
            if packets:
                self._pending.extend(packets[1:])
                return packets[0]

    async def _recv_packet(self) -> Packet:
        raw = await self._next_raw()
        header, payload = decode_tlink(raw)
        if not self.header:
            self.header = header  # echo the panel's header on every send
        if self.decrypt_inbound:
            payload = self._encryption.decrypt(payload)
        message = remove_framing(payload)
        packet = Packet.from_message(message)
        self.remote_seq = packet.sender_seq
        return packet

    async def _send_packet(
        self, sender_seq: int, receiver_seq: int, body: bytes
    ) -> None:
        plaintext = add_framing(bytes((sender_seq, receiver_seq)) + body)
        if self.encrypt_outbound:
            plaintext = self._encryption.encrypt(plaintext)
        self._writer.write(encode_tlink(self.header, plaintext))
        await self._writer.drain()

    async def _send_ack(self, remote_sender_seq: int) -> None:
        await self._send_packet(self.local_seq, remote_sender_seq, b"")

    def _next_local_seq(self) -> int:
        self.local_seq = (self.local_seq + 1) & 0xFF
        return self.local_seq

    def _next_command_seq(self) -> int:
        self.command_seq = (self.command_seq + 1) & 0xFF
        return self.command_seq

    # --- handshake ---
    async def handshake(self) -> str:
        """Run the OpenSession/RequestAccess handshake; returns the header id.

        Raises :class:`HandshakeError` on any protocol or credential failure.
        """
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self._handshake()
        except TimeoutError as err:
            msg = "handshake timed out"
            raise HandshakeError(msg) from err
        except EncryptionError as err:
            raise HandshakeError(str(err)) from err
        except (ValueError, IndexError) as err:
            raise HandshakeError(f"malformed handshake packet: {err}") from err
        except SessionClosedError:
            raise
        self._handshake_done = True
        return self.integration_id

    async def _handshake(self) -> None:
        # 1. Receive OpenSession (plaintext) - panel is the initiator.
        open_pkt = await self._recv_packet()
        if open_pkt.cmd != messages.CMD_OPEN_SESSION:
            msg = f"expected OpenSession, got cmd 0x{open_pkt.cmd:04X}"
            raise HandshakeError(msg)
        enc_type = open_pkt.data[14]
        if enc_type != self._encryption.type_code:
            msg = (
                f"panel wants encryption type {enc_type}, but this server is "
                f"configured for type {self._encryption.type_code}"
            )
            raise HandshakeError(msg)
        self.command_seq = open_pkt.data[0]

        # 2. CommandResponse (plaintext) + 3. SimpleAck (plaintext).
        await self._send_packet(
            self.local_seq,
            open_pkt.sender_seq,
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((self.command_seq, 0x00)),
        )
        await self._recv_packet()

        # 4. Echo OpenSession (plaintext) + 5. CommandResponse + 6. SimpleAck.
        self.command_seq = (self.command_seq + 1) & 0xFF
        self.local_seq = (self.local_seq + 1) & 0xFF
        echoed = (
            u16(messages.CMD_OPEN_SESSION)
            + bytes((self.command_seq,))
            + open_pkt.data[1:]
        )
        await self._send_packet(self.local_seq, self.remote_seq, echoed)
        resp = await self._recv_packet()
        await self._send_ack(resp.sender_seq)

        # 7. Receive RequestAccess (plaintext); its initializer seeds OUTBOUND.
        request_access = await self._recv_packet()
        if request_access.cmd != messages.CMD_REQUEST_ACCESS:
            msg = f"expected RequestAccess, got cmd 0x{request_access.cmd:04X}"
            raise HandshakeError(msg)
        ra_cmd_seq = request_access.data[0]
        init_len = request_access.data[1]
        remote_initializer = request_access.data[2 : 2 + init_len]
        self._encryption.configure_outbound(remote_initializer)
        self.encrypt_outbound = True

        # 8. CommandResponse (ENCRYPTED) + 9. SimpleAck (still plaintext in).
        await self._send_packet(
            self.local_seq,
            request_access.sender_seq,
            u16(messages.CMD_COMMAND_RESPONSE) + bytes((ra_cmd_seq, 0x00)),
        )
        await self._recv_packet()

        # 10. RequestAccess (encrypted); our initializer seeds the panel's
        #     outbound (our inbound). Inbound decryption activates here.
        self.command_seq = (self.command_seq + 1) & 0xFF
        self.local_seq = (self.local_seq + 1) & 0xFF
        our_initializer = self._encryption.configure_inbound()
        self.decrypt_inbound = True
        body = (
            u16(messages.CMD_REQUEST_ACCESS)
            + bytes((self.command_seq, len(our_initializer)))
            + our_initializer
        )
        await self._send_packet(self.local_seq, self.remote_seq, body)

        # 11. CommandResponse (encrypted) + 12. SimpleAck (encrypted).
        resp = await self._recv_packet()
        await self._send_ack(resp.sender_seq)

    # --- receive loop ---
    async def run(self) -> None:
        """Receive packets until the panel disconnects.

        Acks every non-ack packet, resolves waiters, and dispatches
        notifications (unwrapping ``MultipleMessagePacket`` batches).
        Raises :class:`SessionClosedError` when the connection ends.
        """
        try:
            while True:
                packet = await self._recv_packet()
                if packet.is_ack:
                    continue
                await self._send_ack(packet.sender_seq)
                if packet.cmd == messages.CMD_MULTIPLE_MESSAGE_PACKET:
                    self._dispatch_multiple(packet)
                    continue
                self._dispatch(packet)
        except SessionClosedError:
            raise
        except (ConnectionError, OSError, EncryptionError, ValueError) as err:
            raise SessionClosedError(f"session error: {err}") from err

    def _dispatch_multiple(self, packet: Packet) -> None:
        """Unwrap a ``MultipleMessagePacket`` (0x0623) and dispatch each item.

        The panel batches events that occur close together (e.g. a zone
        open immediately followed by its close) into one container
        packet; each embedded sub-message carries its own command word.
        """
        try:
            embedded = messages.decode_multiple_messages(packet.data)
        except ValueError as err:
            _LOGGER.warning("Malformed MultipleMessagePacket ignored: %s", err)
            return
        for cmd, data in embedded:
            if cmd == 0:  # embedded SimpleAck
                continue
            _LOGGER.debug("Dispatching embedded message 0x%04X", cmd)
            self._dispatch(
                Packet(packet.sender_seq, packet.receiver_seq, cmd, data, False)
            )

    def _dispatch(self, packet: Packet) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.future.done():
            # The standalone response the query is waiting for.
            if waiter.expected_cmd is not None and packet.cmd == waiter.expected_cmd:
                waiter.future.set_result(packet)
                return
            if (
                packet.cmd == messages.CMD_COMMAND_RESPONSE
                and packet.data
                and packet.data[0] == waiter.command_seq
            ):
                code = packet.data[1] if len(packet.data) > 1 else 0
                if waiter.resolve_on_response:
                    # command(): any CommandResponse resolves the wait.
                    waiter.future.set_result(packet)
                    return
                if code != 0:
                    # query(): an error response ends the wait; success
                    # responses are just "accepted, data coming" and the
                    # wait continues for the standalone response.
                    waiter.future.set_result(packet)
                    return
                return
        if self.on_notification is not None:
            self.on_notification(packet.cmd, packet.data)

    # --- request/response API ---
    async def _request(
        self,
        embedded_cmd: int,
        payload: bytes,
        *,
        expect_cmd: int | None,
        resolve_on_response: bool,
        timeout: float | None,
    ) -> Packet:
        if self._closing:
            raise SessionClosedError("session is closing")
        async with self._request_lock:
            command_seq = self._next_command_seq()
            waiter = _Waiter(expect_cmd, command_seq, resolve_on_response)
            self._waiter = waiter
            body = messages.command_request_body(command_seq, embedded_cmd, payload)
            try:
                await self._send_packet(self._next_local_seq(), self.remote_seq, body)
                async with asyncio.timeout(timeout or self._request_timeout):
                    return await waiter.future
            except TimeoutError as err:
                raise PanelTimeoutError(
                    f"no response to command 0x{embedded_cmd:04X}"
                ) from err
            finally:
                self._waiter = None

    async def query(
        self, embedded_cmd: int, payload: bytes, *, timeout: float | None = None
    ) -> bytes:
        """Send an embedded query and await its standalone response."""
        packet = await self._request(
            embedded_cmd,
            payload,
            expect_cmd=embedded_cmd,
            resolve_on_response=False,
            timeout=timeout,
        )
        if packet.cmd == messages.CMD_COMMAND_RESPONSE:
            code = packet.data[1] if len(packet.data) > 1 else 0
            raise PanelCommandError(code)
        return packet.data

    async def command(
        self, embedded_cmd: int, payload: bytes, *, timeout: float | None = None
    ) -> None:
        """Send an embedded command and await its CommandResponse."""
        packet = await self._request(
            embedded_cmd,
            payload,
            expect_cmd=None,
            resolve_on_response=True,
            timeout=timeout,
        )
        if packet.cmd == messages.CMD_COMMAND_RESPONSE:
            code = packet.data[1] if len(packet.data) > 1 else 0
            if code != 0:
                raise PanelCommandError(code)
            return
        # Panel answered with the embedded type instead of a CommandResponse.
        return

    # --- keepalive / shutdown ---
    def start_keepalive(self) -> None:
        """Start the connection-poll keepalive loop (100 s interval)."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="tlink-keepalive"
            )

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._closing:
                    return
                await self._send_packet(
                    self._next_local_seq(), self.remote_seq, messages.poll_body()
                )
        except ConnectionError, OSError, SessionClosedError:
            _LOGGER.debug("Keepalive loop ended")

    async def close(self) -> None:
        """End the session: EndSession packet (best effort) + transport close."""
        self._closing = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._handshake_done:
            try:
                async with asyncio.timeout(2.0):
                    await self._send_packet(
                        self._next_local_seq(),
                        self.remote_seq,
                        messages.end_session_body(),
                    )
            except TimeoutError, ConnectionError, OSError, EncryptionError:
                _LOGGER.debug("Failed to send EndSession (transport may be closed)")
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError, OSError:
            _LOGGER.debug("Transport close raised", exc_info=True)
