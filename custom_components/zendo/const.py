"""Constants for the integration."""

import json
from pathlib import Path

DOMAIN = "zendo"
API_VERSION = "1"

try:
    _MANIFEST = json.loads((Path(__file__).parent / "manifest.json").read_text())
    INTEGRATION_VERSION: str = _MANIFEST["version"]
except (FileNotFoundError, KeyError, json.JSONDecodeError):
    INTEGRATION_VERSION = "unknown"

CONF_PUSH_NOTIFICATION_TOKEN = "push_notification_token"
CONF_REFRESH_TIMESTAMPS = "refresh_timestamps"
CONF_CACHED_PROFILES = "cached_profiles"
CONF_CACHED_DEEP_LINK_DESTINATIONS = "cached_deep_link_destinations"
CONF_MANAGED_DEVICES = "managed_devices"
# The Zendo site this Home Assistant belongs to, stored with its push token. Set
# once: by zendo/v2/set_up, or from the push token an older version stored.
CONF_LINKED_SITE_ID = "linked_site_id"

# The identifier of the HA device that groups the status sensor, people and
# device groups. Never a valid managed device ID.
SERVICE_DEVICE_IDENTIFIER = "service"

SIGNAL_CONFIG_UPDATED = f"{DOMAIN}_config_updated"
SIGNAL_PROFILES_UPDATED = f"{DOMAIN}_profiles_updated"

DAILY_REFRESH_LIMIT = 10
