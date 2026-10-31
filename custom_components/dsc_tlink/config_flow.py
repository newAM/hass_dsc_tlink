"""Config flow for the DSC TLink integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PORT

from .const import (
    CONF_ACCESS_CODE,
    CONF_ENCRYPTION_TYPE,
    CONF_INTEGRATION_ID,
    CONF_PANEL_CODE,
    DEFAULT_ENCRYPTION_TYPE,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_ENCRYPTION_TYPE, default=DEFAULT_ENCRYPTION_TYPE): vol.In(
            {1: "Type 1 ([851][423] + Integration ID)", 2: "Type 2 ([851][700])"}
        ),
        vol.Required(CONF_ACCESS_CODE): str,
        vol.Optional(CONF_INTEGRATION_ID): str,
        vol.Optional(CONF_PANEL_CODE): str,
    }
)


def _validate_credentials(data: dict[str, Any]) -> str | None:
    """Return an error key if the encryption credentials are malformed."""
    access_code = data[CONF_ACCESS_CODE].strip()
    encryption_type = int(data[CONF_ENCRYPTION_TYPE])
    integration_id = (data.get(CONF_INTEGRATION_ID) or "").strip()

    if encryption_type == 1:
        if len(access_code) < 8 or not access_code.isdigit():
            return "invalid_access_code"
        if len(integration_id) < 8 or not integration_id.isdigit():
            return "invalid_integration_id"
    elif len(access_code) != 32:
        return "invalid_access_code"
    else:
        try:
            bytes.fromhex(access_code)
        except ValueError:
            return "invalid_access_code"
    return None


async def _async_check_port_free(port: int) -> str | None:
    """Try to bind the port; return an error key if it is unavailable."""
    try:
        # Bind on all interfaces exactly like the server itself will.
        server = await asyncio.start_server(
            None,
            "0.0.0.0",  # noqa: S104 - panels connect from the LAN
            port,
        )
    except OSError:
        return "port_in_use"
    server.close()
    await server.wait_closed()
    return None


class DscTLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the DSC TLink config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_ENCRYPTION_TYPE] = int(user_input[CONF_ENCRYPTION_TYPE])
            user_input[CONF_ACCESS_CODE] = user_input[CONF_ACCESS_CODE].strip()
            if (error := _validate_credentials(user_input)) or (
                error := await _async_check_port_free(user_input[CONF_PORT])
            ):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"DSC panel server (port {user_input[CONF_PORT]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
