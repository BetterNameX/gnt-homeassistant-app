"""The Zendo integration."""

import json
import logging
import time

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api_client import fetch_profiles, send_notification
from .const import (
    CONF_CACHED_DEEP_LINK_DESTINATIONS,
    CONF_CACHED_PROFILES,
    CONF_PUSH_NOTIFICATION_TOKEN,
    CONF_REFRESH_TIMESTAMPS,
    DAILY_REFRESH_LIMIT,
    DOMAIN,
    SIGNAL_CONFIG_UPDATED,
    SIGNAL_PROFILES_UPDATED,
)
from .notify import ZendoNotifyEntity

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NOTIFY]

SECONDS_IN_DAY = 86400

# --- Service names ---

SERVICE_SETUP_PUSH_NOTIFICATIONS = "setup_push_notifications"
SERVICE_REFRESH_PROFILES = "refresh_profiles"
SERVICE_SEND_NOTIFICATION = "send_notification"
SERVICE_SET_DEEP_LINK_DESTINATIONS = "set_deep_link_destinations"
SERVICE_LIST_DEEP_LINK_DESTINATIONS = "list_deep_link_destinations"

STATIC_SERVICES = [
    SERVICE_SETUP_PUSH_NOTIFICATIONS,
    SERVICE_REFRESH_PROFILES,
    SERVICE_SEND_NOTIFICATION,
    SERVICE_SET_DEEP_LINK_DESTINATIONS,
    SERVICE_LIST_DEEP_LINK_DESTINATIONS,
]

# --- Service schemas ---

SERVICE_SETUP_PUSH_NOTIFICATIONS_SCHEMA = vol.Schema(
    {vol.Required("token"): vol.All(cv.string, vol.Length(min=1))}
)

SERVICE_REFRESH_PROFILES_SCHEMA = vol.Schema({})

SERVICE_SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("message"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("interruption_level"): vol.In(["time_sensitive", "critical"]),
        vol.Optional("deep_link_destination"): cv.string,
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_SET_DEEP_LINK_DESTINATIONS_SCHEMA = vol.Schema(
    {vol.Required("destinations"): vol.All(cv.string, vol.Length(min=1))}
)

SERVICE_LIST_DEEP_LINK_DESTINATIONS_SCHEMA = vol.Schema({})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token_or_raise(hass: HomeAssistant) -> str:
    """Return the stored push notification token, or raise if not configured."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Zendo integration is not configured")

    token = entries[0].data.get(CONF_PUSH_NOTIFICATION_TOKEN)

    if not isinstance(token, str) or not token:
        raise HomeAssistantError(
            "Push notifications have not been enabled. "
            "Please open the Zendo iOS/Android app to enable push notifications."
        )

    return token


def _build_notification(
    profile_id: str,
    message: str,
    interruption_level: str | None,
    deep_link_uri: str | None = None,
) -> dict:
    """Build a single GraphQL notification input dict."""
    notification: dict = {
        "profileId": profile_id,
        "body": {"en": message.strip()},
    }

    if interruption_level == "time_sensitive":
        notification["interruptionLevel"] = "TIME_SENSITIVE"
    elif interruption_level == "critical":
        notification["interruptionLevel"] = "CRITICAL"

    if deep_link_uri:
        notification["linking"] = {
            "type": "DEEPLINK_V1",
            "value": deep_link_uri,
        }

    return notification


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Zendo from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN]["notify_entities"] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Shared refresh logic ---

    async def _refresh_and_register(token: str) -> None:
        """Fetch profiles from the backend and create notify entities."""
        entries = hass.config_entries.async_entries(DOMAIN)
        target_entry = entries[0]

        # Rate limiting
        now = time.time()
        stored: list[float] = list(target_entry.data.get(CONF_REFRESH_TIMESTAMPS, []))
        stored = [t for t in stored if now - t < SECONDS_IN_DAY]

        if len(stored) >= DAILY_REFRESH_LIMIT:
            raise HomeAssistantError(
                "Rate limit exceeded. Please try again later."
            )

        profiles = await fetch_profiles(hass, token)
        stored.append(now)

        # Persist timestamps and profiles
        hass.config_entries.async_update_entry(
            target_entry,
            data={
                **target_entry.data,
                CONF_REFRESH_TIMESTAMPS: stored,
                CONF_CACHED_PROFILES: profiles,
            },
        )

        # Add new notify entities (HA deduplicates by unique_id)
        add_entities = hass.data[DOMAIN].get("async_add_notify_entities")

        if add_entities:
            add_entities(
                [
                    ZendoNotifyEntity(target_entry, p["id"], p["label"])
                    for p in profiles
                ],
                update_before_add=True,
            )

        async_dispatcher_send(hass, SIGNAL_PROFILES_UPDATED)
        async_dispatcher_send(hass, SIGNAL_CONFIG_UPDATED)

    # --- setup_push_notifications ---

    async def handle_setup_push_notifications(call: ServiceCall) -> None:
        """Store the JWT token and refresh people."""
        token = call.data["token"]

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Zendo integration is not configured")

        target_entry = entries[0]
        hass.config_entries.async_update_entry(
            target_entry,
            data={**target_entry.data, CONF_PUSH_NOTIFICATION_TOKEN: token},
        )

        async_dispatcher_send(hass, SIGNAL_CONFIG_UPDATED)

        await _refresh_and_register(token)

    # --- refresh_profiles ---

    async def handle_refresh_profiles(call: ServiceCall) -> None:
        """Fetch profiles from the backend and create notify entities."""
        token = _get_token_or_raise(hass)
        await _refresh_and_register(token)

    # --- send_notification ---

    async def handle_send_notification(call: ServiceCall) -> None:
        """Send a push notification to targeted notify entities."""
        token = _get_token_or_raise(hass)

        entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
        message: str = call.data["message"]
        interruption_level: str | None = call.data.get("interruption_level")
        deep_link_destination_id: str | None = call.data.get("deep_link_destination")

        # Resolve deep link destination ID to its URI
        deep_link_uri: str | None = None
        if deep_link_destination_id:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                cached: list[dict] = entries[0].data.get(
                    CONF_CACHED_DEEP_LINK_DESTINATIONS, []
                )
                dest = next(
                    (d for d in cached if d.get("id") == deep_link_destination_id),
                    None,
                )
                if dest:
                    deep_link_uri = dest.get("link")
                else:
                    _LOGGER.warning(
                        "Deep link destination '%s' not found in cached destinations",
                        deep_link_destination_id,
                    )

        entity_map: dict[str, str] = hass.data[DOMAIN].get("notify_entities", {})
        notifications = []

        for entity_id in entity_ids:
            profile_id = entity_map.get(entity_id)

            if profile_id is None:
                raise HomeAssistantError(
                    f'Entity "{entity_id}" is not a Zendo notify entity. '
                    "Please select a valid Zendo person."
                )

            notifications.append(
                _build_notification(
                    profile_id, message, interruption_level, deep_link_uri
                )
            )

        if not notifications:
            raise HomeAssistantError("No valid entities targeted.")

        await send_notification(hass, token, notifications)

    # --- set_deep_link_destinations ---

    async def handle_set_deep_link_destinations(call: ServiceCall) -> None:
        """Store the list of deep link destinations from the mobile app."""
        raw = call.data["destinations"]
        try:
            destinations = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as err:
            raise HomeAssistantError(
                f"Invalid JSON in destinations: {err}"
            ) from err

        if not isinstance(destinations, list):
            raise HomeAssistantError("destinations must be a JSON array")

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Zendo integration is not configured")

        target_entry = entries[0]
        hass.config_entries.async_update_entry(
            target_entry,
            data={
                **target_entry.data,
                CONF_CACHED_DEEP_LINK_DESTINATIONS: destinations,
            },
        )

    # --- list_deep_link_destinations ---

    async def handle_list_deep_link_destinations(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Return stored deep link destinations."""
        entries = hass.config_entries.async_entries(DOMAIN)
        destinations: list[dict] = []
        if entries:
            destinations = entries[0].data.get(
                CONF_CACHED_DEEP_LINK_DESTINATIONS, []
            )
        return {"destinations": destinations}

    # --- Register all services ---

    service_map = {
        SERVICE_SETUP_PUSH_NOTIFICATIONS: (
            handle_setup_push_notifications,
            SERVICE_SETUP_PUSH_NOTIFICATIONS_SCHEMA,
        ),
        SERVICE_REFRESH_PROFILES: (
            handle_refresh_profiles,
            SERVICE_REFRESH_PROFILES_SCHEMA,
        ),
        SERVICE_SEND_NOTIFICATION: (
            handle_send_notification,
            SERVICE_SEND_NOTIFICATION_SCHEMA,
        ),
        SERVICE_SET_DEEP_LINK_DESTINATIONS: (
            handle_set_deep_link_destinations,
            SERVICE_SET_DEEP_LINK_DESTINATIONS_SCHEMA,
        ),
    }

    for name, (handler, schema) in service_map.items():
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

    # list_deep_link_destinations uses SupportsResponse
    if not hass.services.has_service(DOMAIN, SERVICE_LIST_DEEP_LINK_DESTINATIONS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_LIST_DEEP_LINK_DESTINATIONS,
            handle_list_deep_link_destinations,
            schema=SERVICE_LIST_DEEP_LINK_DESTINATIONS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        for name in STATIC_SERVICES:
            hass.services.async_remove(DOMAIN, name)

        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop("notify_entities", None)
        hass.data[DOMAIN].pop("async_add_notify_entities", None)

    return unload_ok
