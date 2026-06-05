"""The Zendo integration."""

import json
import logging
import time

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE = "notify_site_config_manifest_update"
SERVICE_RING_DOORBELL = "ring_doorbell"
SERVICE_DOORBELL_ACTION = "doorbell_action"

STATIC_SERVICES = [
    SERVICE_SETUP_PUSH_NOTIFICATIONS,
    SERVICE_REFRESH_PROFILES,
    SERVICE_SEND_NOTIFICATION,
    SERVICE_SET_DEEP_LINK_DESTINATIONS,
    SERVICE_LIST_DEEP_LINK_DESTINATIONS,
    SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE,
    SERVICE_RING_DOORBELL,
    SERVICE_DOORBELL_ACTION,
]

# The doorbell ring sound plays a configurable number of times (1-5, default 1).
DOORBELL_REPEAT_MIN = 1
DOORBELL_REPEAT_MAX = 5
DOORBELL_REPEAT_DEFAULT = 1

# Doorbell rings may only target a camera destination (Answer opens its stream).
DOORBELL_ALLOWED_DESTINATION = "security_camera"

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

SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE_SCHEMA = vol.Schema(
    {vol.Required("payload"): vol.All(cv.string, vol.Length(min=1))}
)

SERVICE_RING_DOORBELL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("deep_link_destination"): vol.All(cv.string, vol.Length(min=1)),
        # Free-text sound id: the app falls back to a default for anything it
        # doesn't recognise, so we deliberately don't constrain it server-side.
        vol.Optional("sound"): cv.string,
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("repeat", default=DOORBELL_REPEAT_DEFAULT): vol.All(
            vol.Coerce(int), vol.Range(min=DOORBELL_REPEAT_MIN, max=DOORBELL_REPEAT_MAX)
        ),
    },
    extra=vol.REMOVE_EXTRA,
)

# `action` is intentionally an unconstrained string (not an enum) so future actions
# (e.g. snooze, forward) can be broadcast without releasing the companion apps.
SERVICE_DOORBELL_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("event_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("action"): cv.string,
    }
)


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

    # --- notify_site_config_manifest_update ---

    async def handle_notify_site_config_manifest_update(call: ServiceCall) -> None:
        """Broadcast a site config manifest update event to connected clients."""
        payload = call.data.get("payload")
        hass.bus.async_fire("zendo_site_config_manifest_updated", {"payload": payload})

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

    # --- ring_doorbell ---

    async def handle_ring_doorbell(call: ServiceCall) -> None:
        """Fire an in-app doorbell ring to each targeted person.

        Fires one ``zendo_doorbell_ring`` per targeted profile (same recipient
        model as ``send_notification``). The ``eventId`` is the chosen deep link
        destination's id (stable per camera/doorbell, so a later stop correlates
        across every device that rang), and the ``deepLink`` is that destination's
        resolved link. Only ``security_camera`` destinations are allowed.
        """
        entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
        destination_id: str = call.data["deep_link_destination"]
        sound: str | None = call.data.get("sound")
        volume: int | None = call.data.get("volume")
        repeat: int = call.data["repeat"]

        # Resolve the deep link destination to its cached entry
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Zendo integration is not configured")

        cached: list[dict] = entries[0].data.get(
            CONF_CACHED_DEEP_LINK_DESTINATIONS, []
        )
        dest = next((d for d in cached if d.get("id") == destination_id), None)

        if dest is None:
            raise ServiceValidationError(
                f"Deep link destination '{destination_id}' not found. "
                "Use the list_deep_link_destinations service to see available IDs."
            )

        # Only camera destinations are allowed (Answer opens the camera stream)
        if dest.get("destination") != DOORBELL_ALLOWED_DESTINATION:
            raise ServiceValidationError(
                "Doorbell rings can only target a security camera. "
                f"'{destination_id}' is a '{dest.get('destination')}' destination."
            )

        deep_link_uri = dest.get("link")
        if not deep_link_uri:
            raise ServiceValidationError(
                f"Deep link destination '{destination_id}' has no link."
            )

        # Resolve targeted notify entities to their profile IDs
        entity_map: dict[str, str] = hass.data[DOMAIN].get("notify_entities", {})
        profile_ids: list[str] = []

        for entity_id in entity_ids:
            profile_id = entity_map.get(entity_id)

            if profile_id is None:
                raise HomeAssistantError(
                    f'Entity "{entity_id}" is not a Zendo notify entity. '
                    "Please select a valid Zendo person."
                )

            if profile_id not in profile_ids:
                profile_ids.append(profile_id)

        if not profile_ids:
            raise HomeAssistantError("No valid people targeted.")

        # Fire one ring per targeted profile (same eventId/deepLink, different profileId)
        for profile_id in profile_ids:
            payload: dict = {
                "eventId": destination_id,
                "profileId": profile_id,
                "deepLink": deep_link_uri,
                "repeat": repeat,
            }

            if sound:
                payload["sound"] = sound

            if volume is not None:
                payload["volume"] = volume

            hass.bus.async_fire(
                "zendo_doorbell_ring", {"payload": json.dumps(payload)}
            )

    # --- doorbell_action ---

    async def handle_doorbell_action(call: ServiceCall) -> None:
        """Broadcast a doorbell action to connected clients.

        ``action`` is forwarded as-is (free string, not an enum) so future
        actions can be introduced without releasing the companion apps. The app
        types the known set (silence/answer/decline) and ignores anything else.
        """
        event_id: str = call.data["event_id"]
        action: str | None = call.data.get("action")

        payload: dict = {"eventId": event_id}
        if action is not None:
            payload["action"] = action

        hass.bus.async_fire("zendo_doorbell_action", {"payload": json.dumps(payload)})

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
        SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE: (
            handle_notify_site_config_manifest_update,
            SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE_SCHEMA,
        ),
        SERVICE_RING_DOORBELL: (
            handle_ring_doorbell,
            SERVICE_RING_DOORBELL_SCHEMA,
        ),
        SERVICE_DOORBELL_ACTION: (
            handle_doorbell_action,
            SERVICE_DOORBELL_ACTION_SCHEMA,
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
