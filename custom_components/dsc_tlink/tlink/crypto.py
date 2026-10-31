"""ITv2 encryption: AES-ECB with Type 1 / Type 2 key derivation.

The ITv2 handshake negotiates AES-ECB (zero-padded, matching .NET's
``PaddingMode.Zeros``). The two key-derivation schemes are:

* Type 1 - access code from installer section [851][423] and Integration ID
  from [851][422]. Each key is the first 8 characters of the code repeated
  four times, read as hex. The panel's RequestAccess initializer carries a
  check-byte interleaved random whose other half becomes the session key.
* Type 2 - access code from [851][700] (32 hex characters). The session key
  is simply AES(access_key, initializer).

Each direction has its own key: our outbound key is derived from the
initializer the panel sends us; our inbound key is derived from the
initializer we send to the panel.
"""

from __future__ import annotations

import os
from collections.abc import Callable

try:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    def _aes_ecb(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
        cipher = Cipher(
            algorithms.AES(key),
            modes.ECB(),  # noqa: S305 - ECB is the panel's ITv2 protocol
        )
        handler = cipher.encryptor() if encrypt else cipher.decryptor()
        return handler.update(data) + handler.finalize()

except ImportError:  # pragma: no cover - exercised only without cryptography
    from Crypto.Cipher import AES

    def _aes_ecb(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
        box = AES.new(key, AES.MODE_ECB)
        return box.encrypt(data) if encrypt else box.decrypt(data)


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-ECB encrypt with zero padding to the block size."""
    pad = (-len(data)) % 16
    return _aes_ecb(key, data + b"\x00" * pad, encrypt=True)


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-ECB decrypt (padding is left in place; callers strip it)."""
    return _aes_ecb(key, data, encrypt=False)


def _repeat_as_hex(code: str) -> bytes:
    """Type-1 key transform: first 8 chars repeated x4, read as hex."""
    return bytes.fromhex(code[:8] * 4)


class EncryptionError(Exception):
    """Raised when an initializer fails its integrity check or keys are bad."""


class Encryption:
    """Base class for the two ITv2 key-derivation types."""

    type_code: int

    def __init__(self) -> None:
        self.outbound: bytes | None = None
        self.inbound: bytes | None = None
        self.rand: Callable[[int], bytes] = os.urandom

    def configure_outbound(self, remote_initializer: bytes) -> None:
        raise NotImplementedError

    def configure_inbound(self) -> bytes:
        raise NotImplementedError

    def encrypt(self, data: bytes) -> bytes:
        if self.outbound is None:
            msg = "outbound key not configured"
            raise EncryptionError(msg)
        return aes_ecb_encrypt(self.outbound, data)

    def decrypt(self, data: bytes) -> bytes:
        if self.inbound is None:
            msg = "inbound key not configured"
            raise EncryptionError(msg)
        return aes_ecb_decrypt(self.inbound, data)


class Type1Encryption(Encryption):
    """Access code [851][423], checked against Integration ID [851][422].

    The two directions authenticate different credentials: the initializer a
    side *sends* is encrypted with that side's "send key" (the access code
    for the hub/NeoHub role), while the initializer it *receives* is decrypted
    with the other credential (the Integration ID). The panel mirrors this
    pairing, so ``panel_role=True`` swaps the two keys.
    """

    type_code = 1

    def __init__(
        self, access_code: str, integration_id: str, *, panel_role: bool = False
    ) -> None:
        super().__init__()
        try:
            self.access_key = _repeat_as_hex(access_code)
            self.id_key = _repeat_as_hex(integration_id)
        except ValueError as err:
            msg = f"Type 1 access code / integration ID must be hex: {err}"
            raise EncryptionError(msg) from err
        if panel_role:
            self._send_key = self.id_key
            self._receive_key = self.access_key
        else:
            self._send_key = self.access_key
            self._receive_key = self.id_key

    def configure_outbound(self, remote_initializer: bytes) -> None:
        if len(remote_initializer) != 48:
            msg = "Type 1 initializer must be 48 bytes"
            raise EncryptionError(msg)
        check_bytes = remote_initializer[:16]
        cipher_text = remote_initializer[16:48]
        plain = aes_ecb_decrypt(self._receive_key, cipher_text)
        if check_bytes != plain[0::2]:
            msg = "Type 1 initializer check-byte failure (wrong Integration ID?)"
            raise EncryptionError(msg)
        self.outbound = plain[1::2]

    def configure_inbound(self) -> bytes:
        random = self.rand(32)
        check_bytes = random[0::2]
        self.inbound = random[1::2]
        return check_bytes + aes_ecb_encrypt(self._send_key, random)


class Type2Encryption(Encryption):
    """Access code [851][700]; session key = AES(access_key, initializer)."""

    type_code = 2

    def __init__(self, access_code: str) -> None:
        super().__init__()
        try:
            self.access_key = bytes.fromhex(access_code)
        except ValueError as err:
            msg = "Type 2 access code must be 32 hex characters"
            raise EncryptionError(msg) from err
        if len(self.access_key) != 16:
            msg = "Type 2 access code must be 32 hex characters"
            raise EncryptionError(msg)

    def configure_outbound(self, remote_initializer: bytes) -> None:
        if len(remote_initializer) != 16:
            msg = "Type 2 initializer must be 16 bytes"
            raise EncryptionError(msg)
        self.outbound = aes_ecb_encrypt(self.access_key, remote_initializer)

    def configure_inbound(self) -> bytes:
        local = self.rand(16)
        self.inbound = aes_ecb_encrypt(self.access_key, local)
        return local
