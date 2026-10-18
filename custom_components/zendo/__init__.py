"""The integration."""

import json
import logging
import time
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service

from .admin_code import (
    SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE_SCHEMA,
    SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET_SCHEMA,
    BNGntAdminCodeStore,
    async_remove_store as async_remove_admin_code_store,
    handle_managed_device_admin_code_configure,
    handle_managed_device_admin_code_get,
    ws_report_managed_device_admin_code,
)
from .api_client import fetch_profiles, send_notification
from .const import (
    CONF_CACHED_DEEP_LINK_DESTINATIONS,
    CONF_CACHED_PROFILES,
    CONF_MANAGED_DEVICES,
    CONF_PUSH_NOTIFICATION_TOKEN,
    CONF_REFRESH_TIMESTAMPS,
    DAILY_REFRESH_LIMIT,
    DOMAIN,
    SERVICE_DEVICE_IDENTIFIER,
    SIGNAL_CONFIG_UPDATED,
    SIGNAL_PROFILES_UPDATED,
)
from .event import BNGntDeviceGroupEventEntity
from .managed_devices import (
    BNGntManagedDeviceError,
    BNGntManagedDeviceRegistry,
    SERVICE_DELETE_MANAGED_DEVICE_SCHEMA,
    SERVICE_LIST_MANAGED_DEVICES_SCHEMA,
    SERVICE_MANAGED_DEVICE_APP_LOCK_SCHEMA,
    SERVICE_MANAGED_DEVICE_APP_RELOAD_SCHEMA,
    SERVICE_MANAGED_DEVICE_APP_UNLOCK_SCHEMA,
    SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE_SCHEMA,
    SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE_SCHEMA,
    SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE_SCHEMA,
    SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE_SCHEMA,
    SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP_SCHEMA,
    SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE_SCHEMA,
    SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE_SCHEMA,
    SERVICE_REGISTER_MANAGED_DEVICE_SCHEMA,
    SERVICE_REPLACE_MANAGED_DEVICE_SCHEMA,
    SERVICE_UNREGISTER_MANAGED_DEVICE_SCHEMA,
    SERVICE_UPDATE_MANAGED_DEVICE_STATE_SCHEMA,
    handle_delete_managed_device,
    handle_list_managed_devices,
    handle_managed_device_app_lock,
    handle_managed_device_app_reload,
    handle_managed_device_app_unlock,
    handle_managed_device_audio_configure,
    handle_managed_device_auto_brightness_configure,
    handle_managed_device_command_response,
    handle_managed_device_screen_configure,
    handle_managed_device_screen_wake_up,
    handle_managed_device_screensaver_configure,
    handle_managed_device_wake_on_approach_configure,
    handle_register_managed_device,
    handle_replace_managed_device,
    handle_unregister_managed_device,
    handle_update_managed_device_state,
)
from .notify import BNGntNotifyEntity
from .profiles import (
    async_sync_profile_entities,
    device_groups,
    people,
    profiles_predate_device_groups,
)
from .settings_backup import (
    BNGntSettingsBackupStore,
    async_remove_store as async_remove_settings_backup_store,
    ws_complete_managed_device_settings_restore,
    ws_get_managed_device_settings_restore,
    ws_report_managed_device_settings_backup,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.NOTIFY,
    Platform.SENSOR,
]

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

SERVICE_REGISTER_MANAGED_DEVICE = "register_managed_device"
SERVICE_UNREGISTER_MANAGED_DEVICE = "unregister_managed_device"
SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE = "managed_device_command_response"
SERVICE_UPDATE_MANAGED_DEVICE_STATE = "update_managed_device_state"
SERVICE_LIST_MANAGED_DEVICES = "list_managed_devices"
SERVICE_REPLACE_MANAGED_DEVICE = "replace_managed_device"
SERVICE_DELETE_MANAGED_DEVICE = "delete_managed_device"
SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE = "managed_device_screensaver_configure"
SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE = "managed_device_screen_configure"
SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP = "managed_device_screen_wake_up"
SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE = "managed_device_audio_configure"
SERVICE_MANAGED_DEVICE_APP_RELOAD = "managed_device_app_reload"
SERVICE_MANAGED_DEVICE_APP_LOCK = "managed_device_app_lock"
SERVICE_MANAGED_DEVICE_APP_UNLOCK = "managed_device_app_unlock"
SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE = (
    "managed_device_auto_brightness_configure"
)
SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE = (
    "managed_device_wake_on_approach_configure"
)
SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE = "managed_device_admin_code_configure"
SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET = "managed_device_admin_code_get"

STATIC_SERVICES = [
    SERVICE_SETUP_PUSH_NOTIFICATIONS,
    SERVICE_REFRESH_PROFILES,
    SERVICE_SEND_NOTIFICATION,
    SERVICE_SET_DEEP_LINK_DESTINATIONS,
    SERVICE_LIST_DEEP_LINK_DESTINATIONS,
    SERVICE_NOTIFY_SITE_CONFIG_MANIFEST_UPDATE,
    SERVICE_RING_DOORBELL,
    SERVICE_DOORBELL_ACTION,
    SERVICE_REGISTER_MANAGED_DEVICE,
    SERVICE_UNREGISTER_MANAGED_DEVICE,
    SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE,
    SERVICE_UPDATE_MANAGED_DEVICE_STATE,
    SERVICE_LIST_MANAGED_DEVICES,
    SERVICE_REPLACE_MANAGED_DEVICE,
    SERVICE_DELETE_MANAGED_DEVICE,
    SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE,
    SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE,
    SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP,
    SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE,
    SERVICE_MANAGED_DEVICE_APP_RELOAD,
    SERVICE_MANAGED_DEVICE_APP_LOCK,
    SERVICE_MANAGED_DEVICE_APP_UNLOCK,
    SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE,
    SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE,
    SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE,
    SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET,
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
# Migration
# ---------------------------------------------------------------------------

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version."""
    if entry.version > 1:
        return False

    # Minor version 2: no unique ID or device identifier contains the entry
    # ID, which a reinstall changes. Each step skips what's already done, so
    # running it twice is safe.
    if entry.minor_version < 2:
        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"

        @callback
        def _strip_entry_id(
            entity_entry: er.RegistryEntry,
        ) -> dict[str, Any] | None:
            if not entity_entry.unique_id.startswith(prefix):
                return None

            new_unique_id = entity_entry.unique_id.removeprefix(prefix)
            if ent_reg.async_get_entity_id(entity_entry.domain, DOMAIN, new_unique_id):
                _LOGGER.warning(
                    "Can't migrate %s to unique ID %s, which another entity has",
                    entity_entry.entity_id,
                    new_unique_id,
                )
                return None

            return {"new_unique_id": new_unique_id}

        await er.async_migrate_entries(hass, entry.entry_id, _strip_entry_id)
        _move_service_device(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)

    return True


@callback
def _move_service_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the service device from the entry ID to its own identifier.

    Changing only the entities' DeviceInfo would add a second service device
    beside this one.
    """
    dev_reg = dr.async_get(hass)
    old_device = dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if old_device is None:
        return

    if dev_reg.async_get_device(identifiers={(DOMAIN, SERVICE_DEVICE_IDENTIFIER)}):
        _LOGGER.warning(
            "Can't move device %s to identifier %s, which another device has",
            old_device.id,
            SERVICE_DEVICE_IDENTIFIER,
        )
        return

    dev_reg.async_update_device(
        old_device.id, new_identifiers={(DOMAIN, SERVICE_DEVICE_IDENTIFIER)}
    )


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN]["notify_entities"] = {}
    hass.data[DOMAIN]["device_group_entities"] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Managed device registry ---

    # Loaded first, so that they drop the entries of records that are gone
    # before anything can report.
    managed_device_ids = list(entry.data.get(CONF_MANAGED_DEVICES, {}))
    admin_codes = BNGntAdminCodeStore(hass)
    await admin_codes.async_load(managed_device_ids)
    settings_backups = BNGntSettingsBackupStore(hass)
    await settings_backups.async_load(managed_device_ids)

    registry = BNGntManagedDeviceRegistry(hass, entry, admin_codes, settings_backups)
    hass.data[DOMAIN]["managed_device_registry"] = registry
    await registry.async_start()

    # --- Shared refresh logic ---

    async def _refresh_and_register(token: str) -> None:
        """Fetch profiles from the backend and sync their entities."""
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

        await async_sync_profile_entities(
            hass.data[DOMAIN]["notify_entities"],
            people(profiles),
            hass.data[DOMAIN].get("async_add_notify_entities"),
            lambda p: BNGntNotifyEntity(target_entry, p["id"], p["label"]),
        )
        await async_sync_profile_entities(
            hass.data[DOMAIN]["device_group_entities"],
            device_groups(profiles),
            hass.data[DOMAIN].get("async_add_device_group_entities"),
            lambda p: BNGntDeviceGroupEventEntity(target_entry, p["id"], p["label"]),
        )

        async_dispatcher_send(hass, SIGNAL_PROFILES_UPDATED)
        async_dispatcher_send(hass, SIGNAL_CONFIG_UPDATED)

    async def _refresh_cached_profiles(token: str) -> None:
        """Refresh the profile cache, logging rather than raising on failure."""
        try:
            await _refresh_and_register(token)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Couldn't refresh the cached profiles, will retry at the next start",
                exc_info=True,
            )

    # --- setup_push_notifications ---

    async def handle_setup_push_notifications(call: ServiceCall) -> None:
        """Store the JWT token and refresh profiles."""
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
        """Fetch profiles from the backend and sync their entities."""
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

        notify_entities: dict[str, BNGntNotifyEntity] = hass.data[DOMAIN].get(
            "notify_entities", {}
        )
        device_group_entities: dict[str, BNGntDeviceGroupEventEntity] = hass.data[
            DOMAIN
        ].get("device_group_entities", {})
        notifications = []

        for entity_id in entity_ids:
            if entity_id in device_group_entities:
                raise ServiceValidationError(
                    f'Entity "{entity_id}" is a Zendo device group. '
                    "Device groups can't receive push notifications."
                )

            notify_entity = notify_entities.get(entity_id)

            if notify_entity is None:
                raise HomeAssistantError(
                    f'Entity "{entity_id}" is not a Zendo notify entity. '
                    "Please select a valid Zendo person."
                )

            notifications.append(
                _build_notification(
                    notify_entity.profile_id,
                    message,
                    interruption_level,
                    deep_link_uri,
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
        hass.bus.async_fire("bngnt_site_config_manifest_updated", {"payload": payload})

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
        """Fire an in-app doorbell ring to each targeted person or device group.

        Targets are notify entities (people) and device group event entities.
        Fires one ``bngnt_doorbell_ring`` per targeted profile. The ``eventId``
        is the chosen deep link destination's id (stable per camera/doorbell, so
        a later stop correlates across every device that rang), and the
        ``deepLink`` is that destination's resolved link. Only
        ``security_camera`` destinations are allowed.
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

        # Resolve targeted entities to their profile IDs
        notify_entities: dict[str, BNGntNotifyEntity] = hass.data[DOMAIN].get(
            "notify_entities", {}
        )
        device_group_entities: dict[str, BNGntDeviceGroupEventEntity] = hass.data[
            DOMAIN
        ].get("device_group_entities", {})
        profile_ids: list[str] = []
        targeted_device_groups: list[BNGntDeviceGroupEventEntity] = []

        for entity_id in entity_ids:
            target = notify_entities.get(entity_id) or device_group_entities.get(
                entity_id
            )

            if target is None:
                raise ServiceValidationError(
                    f'Entity "{entity_id}" is not a Zendo person or device group. '
                    "Please select a Zendo person or device group."
                )

            if isinstance(target, BNGntDeviceGroupEventEntity):
                targeted_device_groups.append(target)

            if target.profile_id not in profile_ids:
                profile_ids.append(target.profile_id)

        if not profile_ids:
            raise ServiceValidationError("No people or device groups targeted.")

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
                "bngnt_doorbell_ring", {"payload": json.dumps(payload)}
            )

        for device_group in targeted_device_groups:
            device_group.async_record_ring(destination_id)

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

        hass.bus.async_fire("bngnt_doorbell_action", {"payload": json.dumps(payload)})

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

    # Managed-device handlers are module-level async functions. We use
    # functools.partial to bind ``hass`` -- unlike a lambda, partial
    # preserves ``iscoroutinefunction`` so HA awaits them correctly.
    managed_device_service_map = {
        SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE: (
            partial(handle_managed_device_command_response, hass),
            SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE: (
            partial(handle_managed_device_screensaver_configure, hass),
            SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE: (
            partial(handle_managed_device_screen_configure, hass),
            SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP: (
            partial(handle_managed_device_screen_wake_up, hass),
            SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE: (
            partial(handle_managed_device_audio_configure, hass),
            SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_APP_RELOAD: (
            partial(handle_managed_device_app_reload, hass),
            SERVICE_MANAGED_DEVICE_APP_RELOAD_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_APP_LOCK: (
            partial(handle_managed_device_app_lock, hass),
            SERVICE_MANAGED_DEVICE_APP_LOCK_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_APP_UNLOCK: (
            partial(handle_managed_device_app_unlock, hass),
            SERVICE_MANAGED_DEVICE_APP_UNLOCK_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE: (
            partial(handle_managed_device_auto_brightness_configure, hass),
            SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE_SCHEMA,
        ),
        SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE: (
            partial(handle_managed_device_wake_on_approach_configure, hass),
            SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE_SCHEMA,
        ),
    }

    for name, (handler, schema) in {**service_map, **managed_device_service_map}.items():
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

    # Services that return responses to the caller
    responsive_services = {
        SERVICE_LIST_DEEP_LINK_DESTINATIONS: (
            handle_list_deep_link_destinations,
            SERVICE_LIST_DEEP_LINK_DESTINATIONS_SCHEMA,
            SupportsResponse.ONLY,
        ),
        SERVICE_REGISTER_MANAGED_DEVICE: (
            partial(handle_register_managed_device, hass),
            SERVICE_REGISTER_MANAGED_DEVICE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        SERVICE_UNREGISTER_MANAGED_DEVICE: (
            partial(handle_unregister_managed_device, hass),
            SERVICE_UNREGISTER_MANAGED_DEVICE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        SERVICE_UPDATE_MANAGED_DEVICE_STATE: (
            partial(handle_update_managed_device_state, hass),
            SERVICE_UPDATE_MANAGED_DEVICE_STATE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        SERVICE_LIST_MANAGED_DEVICES: (
            partial(handle_list_managed_devices, hass),
            SERVICE_LIST_MANAGED_DEVICES_SCHEMA,
            SupportsResponse.ONLY,
        ),
        SERVICE_REPLACE_MANAGED_DEVICE: (
            partial(handle_replace_managed_device, hass),
            SERVICE_REPLACE_MANAGED_DEVICE_SCHEMA,
            SupportsResponse.ONLY,
        ),
        SERVICE_DELETE_MANAGED_DEVICE: (
            partial(handle_delete_managed_device, hass),
            SERVICE_DELETE_MANAGED_DEVICE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
    }

    for name, (handler, schema, response_mode) in responsive_services.items():
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN, name, handler, schema=schema,
                supports_response=response_mode,
            )

    # Only an administrator can read or change an admin code. A call with no
    # user, such as an automation's, is allowed.
    admin_services = {
        SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE: (
            partial(handle_managed_device_admin_code_configure, hass),
            SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE_SCHEMA,
            SupportsResponse.NONE,
        ),
        SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET: (
            partial(handle_managed_device_admin_code_get, hass),
            SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET_SCHEMA,
            SupportsResponse.ONLY,
        ),
    }

    for name, (handler, schema, response_mode) in admin_services.items():
        if not hass.services.has_service(DOMAIN, name):
            async_register_admin_service(
                hass, DOMAIN, name, handler, schema=schema,
                supports_response=response_mode,
            )

    # A WebSocket command can't be unregistered: after an unload, it answers
    # with an error until the entry is set up again.
    websocket_api.async_register_command(hass, ws_report_managed_device_admin_code)
    websocket_api.async_register_command(
        hass, ws_report_managed_device_settings_backup
    )
    websocket_api.async_register_command(hass, ws_get_managed_device_settings_restore)
    websocket_api.async_register_command(
        hass, ws_complete_managed_device_settings_restore
    )

    # --- Profiles cached before device groups ---

    # A cache written before device groups can't tell one from a person, so the
    # platforms have just set any device group up as a person. Refresh it in the
    # background to move them to their own entities. A refreshed cache carries
    # the flag, so this stops once a refresh succeeds.
    token = entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)

    if (
        isinstance(token, str)
        and token
        and profiles_predate_device_groups(entry.data.get(CONF_CACHED_PROFILES, []))
    ):
        entry.async_create_background_task(
            hass,
            _refresh_cached_profiles(token),
            "bngnt_refresh_cached_profiles",
        )

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow the Delete button on an offline managed device.

    On True, HA removes the device and its entities itself.
    """
    if (DOMAIN, SERVICE_DEVICE_IDENTIFIER) in device_entry.identifiers:
        return False

    registry: BNGntManagedDeviceRegistry | None = hass.data.get(DOMAIN, {}).get(
        "managed_device_registry"
    )
    if registry is None:
        return False

    key = registry.resolve_ha_device_to_key(device_entry.id)
    if key is None:
        return True

    try:
        registry.delete_device(key, remove_ha_device=False)
    except BNGntManagedDeviceError as err:
        raise HomeAssistantError(
            "This device is online. "
            "Turn off Always-On Display or sign it out of Zendo first."
        ) from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        registry = hass.data[DOMAIN].pop("managed_device_registry", None)
        if registry:
            registry.stop()
            await registry.admin_codes.async_flush()
            await registry.settings_backups.async_flush()

        for name in STATIC_SERVICES:
            hass.services.async_remove(DOMAIN, name)

        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop("notify_entities", None)
        hass.data[DOMAIN].pop("async_add_notify_entities", None)
        hass.data[DOMAIN].pop("device_group_entities", None)
        hass.data[DOMAIN].pop("async_add_device_group_entities", None)
        hass.data[DOMAIN].pop("async_add_managed_device_sensors", None)
        hass.data[DOMAIN].pop("async_add_managed_device_binary_sensors", None)
        hass.data[DOMAIN].pop("managed_device_entities", None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the admin codes and settings backups kept for the entry."""
    await async_remove_admin_code_store(hass)
    await async_remove_settings_backup_store(hass)
