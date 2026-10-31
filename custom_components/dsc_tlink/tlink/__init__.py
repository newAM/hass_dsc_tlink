"""tlink - a pure-asyncio implementation of the DSC TLink/ITv2 protocol.

This package is vendored inside the ``dsc_tlink`` Home Assistant
integration and has no Home Assistant dependencies, so it can also be used
standalone (see ``tools/fake_panel.py``) and extracted to PyPI later.

Layered structure:

* :mod:`tlink.framing`  - TLink byte stuffing, ITv2 length/CRC framing
* :mod:`tlink.crypto`   - AES-ECB with Type 1 / Type 2 key derivation
* :mod:`tlink.messages` - ITv2 command words, encoders, notification decoders
* :mod:`tlink.session`  - handshake, receive loop, request/response
* :mod:`tlink.state`    - per-panel zone/partition state tracking
* :mod:`tlink.server`   - TCP listener accepting panel connections
"""

from .crypto import Encryption, EncryptionError, Type1Encryption, Type2Encryption
from .messages import ArmingMode
from .server import (
    DEFAULT_PORT,
    DEFAULT_TYPE2_ACCESS_CODE,
    EncryptionConfig,
    PanelHandle,
    TLinkServer,
)
from .session import (
    HandshakeError,
    PanelCommandError,
    PanelSession,
    PanelTimeoutError,
    SessionClosedError,
    TLinkError,
)

__version__ = "2.0.0"

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TYPE2_ACCESS_CODE",
    "ArmingMode",
    "Encryption",
    "EncryptionConfig",
    "EncryptionError",
    "HandshakeError",
    "PanelCommandError",
    "PanelHandle",
    "PanelSession",
    "PanelTimeoutError",
    "SessionClosedError",
    "TLinkError",
    "TLinkServer",
    "Type1Encryption",
    "Type2Encryption",
]
