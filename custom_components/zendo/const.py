"""Constants for the Zendo integration."""

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

SIGNAL_CONFIG_UPDATED = f"{DOMAIN}_config_updated"
SIGNAL_PROFILES_UPDATED = f"{DOMAIN}_profiles_updated"

DAILY_REFRESH_LIMIT = 10
