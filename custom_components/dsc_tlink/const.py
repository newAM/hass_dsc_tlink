"""Constants for the DSC TLink integration."""

DOMAIN = "dsc_tlink"

CONF_ENCRYPTION_TYPE = "encryption_type"
CONF_ACCESS_CODE = "access_code"  # encryption access code ([851][423]/[851][700])
CONF_INTEGRATION_ID = "integration_id"  # type 1 integration ID ([851][422])
CONF_PANEL_CODE = "panel_code"  # panel user code for arm/disarm

DEFAULT_PORT = 3072
DEFAULT_ENCRYPTION_TYPE = 2

MANUFACTURER = "DSC"
PANEL_MODEL = "PowerSeries NEO"

# Dispatcher signals
SIGNAL_SESSION_ADDED = f"{DOMAIN}_session_added"
SIGNAL_CONNECTION_STATE = f"{DOMAIN}_connection_state"
SIGNAL_PARTITION_UPDATED = f"{DOMAIN}_partition_updated"
SIGNAL_ZONE_UPDATED = f"{DOMAIN}_zone_updated"
