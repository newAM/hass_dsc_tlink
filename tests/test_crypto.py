"""Tests for ITv2 encryption (AES-ECB, Type 1 / Type 2 key derivation)."""

from __future__ import annotations

import pytest
from tlink.crypto import (
    EncryptionError,
    Type1Encryption,
    Type2Encryption,
    aes_ecb_decrypt,
    aes_ecb_encrypt,
)

ACCESS_CODE = "12345678123456781234567812345678"


def test_aes_ecb_zero_padding_roundtrip() -> None:
    key = bytes(range(16))
    for data in (b"", b"a", b"exactly-16-bytes", b"17 bytes of data!"):
        encrypted = aes_ecb_encrypt(key, data)
        assert len(encrypted) % 16 == 0
        decrypted = aes_ecb_decrypt(key, encrypted)
        assert decrypted[: len(data)] == data


def test_aes_ecb_known_block() -> None:
    # AES-128 of a zero block under a zero key (well-known vector).
    encrypted = aes_ecb_encrypt(bytes(16), bytes(16))
    assert encrypted == bytes.fromhex("66e94bd4ef8a2c3b884cfa59ca342b2e")


def test_type2_mirrored_parties() -> None:
    """Hub and panel derive matching direction keys from each other's init."""
    hub = Type2Encryption(ACCESS_CODE)
    panel = Type2Encryption(ACCESS_CODE)

    panel_init = panel.configure_inbound()  # panel's random -> seeds keys
    hub.configure_outbound(panel_init)

    hub_init = hub.configure_inbound()
    panel.configure_outbound(hub_init)

    message = b"hello panel, over the encrypted channel..."
    assert panel.decrypt(hub.encrypt(message))[: len(message)] == message
    assert hub.decrypt(panel.encrypt(message))[: len(message)] == message


def test_type2_wrong_code_fails() -> None:
    hub = Type2Encryption(ACCESS_CODE)
    panel = Type2Encryption("ffffffffffffffffffffffffffffffff")
    hub.configure_outbound(panel.configure_inbound())
    garbage = hub.encrypt(b"would not decrypt cleanly")
    # Decrypts to garbage: the plaintext must not survive the round trip.
    assert panel.decrypt(garbage)[:25] != b"would not decrypt cleanl"


def test_type2_bad_access_code_length() -> None:
    with pytest.raises(EncryptionError, match="32 hex"):
        Type2Encryption("1234")


def test_type2_non_hex_access_code() -> None:
    with pytest.raises(EncryptionError):
        Type2Encryption("zz" * 16)


def test_type1_mirrored_parties() -> None:
    code = "42342342"
    integration_id = "11111111"
    hub = Type1Encryption(code, integration_id)
    panel = Type1Encryption(code, integration_id, panel_role=True)
    # Type 1 keys come from the first 8 chars repeated as hex.
    assert hub.access_key == bytes.fromhex(code * 4)

    panel_init = panel.configure_inbound()  # encrypted with the ID key
    hub.configure_outbound(panel_init)
    hub_init = hub.configure_inbound()  # encrypted with the access key
    panel.configure_outbound(hub_init)

    message = b"type one session"
    assert panel.decrypt(hub.encrypt(message))[: len(message)] == message
    assert hub.decrypt(panel.encrypt(message))[: len(message)] == message


def test_type1_wrong_integration_id_rejected() -> None:
    hub = Type1Encryption("42342342", "11111111")
    panel = Type1Encryption("42342342", "99999999", panel_role=True)
    with pytest.raises(EncryptionError, match="check-byte"):
        hub.configure_outbound(panel.configure_inbound())
