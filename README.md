# DSC TLink for Home Assistant

Connects [DSC PowerSeries NEO](https://www.dsc.com) alarm panels to Home
Assistant over the panel's native TLink / ITv2 protocol.

This integration is an AI rewrite of [NeoHub] service and the
[NeoHub Home Assistant integration] to reduce the number of services I run.
Those two components (a .NET bridge service plus a WebSocket client
integration) are collapsed into one Python Home Assistant integration.

[NeoHub Home Assistant integration]: https://github.com/BrianHumlicek/NeoHub-Home-Assistant-Integration

Home Assistant itself listens for the panel: your DSC NEO panel's
communicator is programmed to connect to HA, performs the encrypted
ITv2 handshake, and then pushes real-time zone and partition events.

## Features

- **Local push** no polling; the panel pushes zone open/close, arm/disarm,
  exit delay, ready and bypass events as they happen
- **Alarm control panel** entity per partition (arm away/home/night, disarm)
- **Binary sensor** per zone (with bypass/tamper/fault attributes)
- **Door chime status** per partition
- Zone and partition labels pulled from the panel
- Multiple panels supported (keyed by Integration ID)
- ITv2 encryption types 1 and 2

## Missing Features

This integration covers monitoring and control (arm/disarm, bypass,
zone/partition state). The [NeoHub] service it replaces has capabilities that
are not implemented here:

- **Panel user management** - viewing, adding, editing, enabling and
  disabling access codes
- **Installer configuration** - reading and writing zone definitions,
  zone attributes and real zone-to-partition assignment (zones here are
  assigned to partition 1 heuristically), editing zone/partition labels,
  configuration snapshots and diffing
- **Panel clock tracking** - monitoring the panel's date/time broadcasts
- **Programming-mode tracking** - following panel lead-in/out of
  programming mode
- **Dev console** - composing and sending arbitrary ITv2 messages to a
  live panel

If you need any of the above, keep using [NeoHub] instead of this integration.

## Panel setup

Program the panel's communicator to point at your Home Assistant host:

1. Enter installer programming, section **[851]** (integration groups)
2. Set the destination IP to your Home Assistant host and port **3072**
   (or whichever port you configure below)
3. Configure the encryption credentials:
   - **Type 2** (default): access code from **[851][700]** (32 hex
     characters; factory default `12345678123456781234567812345678`)
   - **Type 1**: access code from **[851][423]** plus the Integration
     Identification Number from **[851][422]**
4. Power-cycle or reconnect the communicator; it will dial Home Assistant

The full wire protocol is documented in
[NeoHub's protocol documentation](https://github.com/BrianHumlicek/NeoHub/blob/master/docs/TLink-ITv2-Protocol.md).

## Installation

### HACS

Add this repository as a custom repository (category _Integration_), then
install **DSC TLink**. Restart Home Assistant.

### Manual

Copy `custom_components/dsc_tlink/` into the `custom_components/` directory
of your Home Assistant configuration and restart.

## Configuration

Settings → Devices & services → Add Integration → **DSC TLink**

| Field                  | Description                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------ |
| Listen port            | TCP port panels connect to (default `3072`)                                                |
| Encryption type        | 2 ([851][700]) or 1 ([851][423] + Integration ID)                                          |
| Encryption access code | The 32-hex (type 2) or 8+ digit (type 1) code                                              |
| Integration ID         | Required for type 1 only ([851][422])                                                      |
| Panel user code        | Optional panel user code (e.g. `1234`) used when arming/disarming without providing a code |

Entities appear automatically a few seconds after the panel connects
(each panel performs a handshake, then a full label/status sync). Every
partition becomes a device with an alarm control panel entity; every
programmed zone becomes a binary sensor.

> **Networking note:** the panel must be able to reach this port. On
> Home Assistant OS/Supervised (and standard `--network=host` container
> installs) the port is directly reachable on the LAN. If you run Home
> Assistant Core in a bridged Docker network, map port 3072 to the host.

## Development

Dependencies are managed with Nix:

```sh
nix develop          # Python 3.14 + pytest/pytest-asyncio/cryptography + ruff
pytest               # framing, crypto, messages, state and end-to-end tests
ruff check .         # lint
ruff format .        # format
```

The test suite includes a full end-to-end run: `tools/fake_panel.py` is a
scripted DSC panel that connects to the server, performs the real
encrypted handshake, answers queries, and emits notifications. It also
works standalone against a live Home Assistant instance for debugging:

```sh
nix develop -c python tools/fake_panel.py --port 3072
nix develop -c python tools/fake_panel.py --port 3072 --toggle-zone 1  # flip a zone every 10s
```

### Repository layout

```
custom_components/dsc_tlink/     Home Assistant integration
  tlink/                         vendored protocol library (no HA deps)
    framing.py                   TLink stuffing + ITv2 length/CRC framing
    crypto.py                    AES-ECB, Type 1/2 key derivation
    messages.py                  command words, encoders, decoders
    session.py                   handshake, receive loop, request/response
    state.py                     zone/partition state tracking
    server.py                    TCP listener + initial sync + command API
tools/fake_panel.py              scripted panel for tests and debugging
tests/                           unit + end-to-end tests
```

## Credits

The protocol implementation is a Python port of the reverse-engineering
work done in the [NeoHub] / [DSC-TLink] by Brian Humlicek and contributors.

[NeoHub]: https://github.com/BrianHumlicek/NeoHub
[DSC-TLink]: https://github.com/BrianHumlicek/DSC-TLink
