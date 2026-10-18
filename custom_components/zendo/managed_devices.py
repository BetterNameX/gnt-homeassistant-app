"""Managed device registry, entity classes, and service handlers.

Identity: the integration owns each managed device. Its record is keyed by
an opaque managed device ID (``key``), which is also its HA device
identifier ``(DOMAIN, key)`` and what automations point at. The app device
currently attached to it is the record's ``profile_id`` + ``device_id``, and
that pair can change: ``replace_managed_device`` attaches another device to
the record. A key is never rebuilt from the attached ids, so never parse
one. See docs/managed-devices.md.

Command/response flow (JSON-RPC 2.0 over the HA event bus):

1. An automation-facing service (e.g. ``managed_device_screen_wake_up``)
   builds a JSON-RPC request with a unique ``id`` (UUID).
2. The request is wrapped in a ``bngnt_managed_device_command`` bus event
   together with the attached ``profile_id`` and ``device_id``.  The
   ``request`` field is a JSON-stringified JSON-RPC 2.0 request object
   (matching the existing pattern: complex payloads are JSON-encoded).
3. The Zendo app receives the event via its WebSocket subscription, filters
   by its own identity, and passes the request to its local JSON-RPC server.
4. The app calls the ``zendo.managed_device_command_response`` HA service
   with the JSON-RPC response (same ``id``), also JSON-stringified.
5. ``handle_command_response`` looks up the pending ``asyncio.Future`` by
   that ``id`` and resolves it, unblocking the original service call.
6. If the device doesn't respond within ``COMMAND_TIMEOUT`` seconds, the
   future is cancelled and the service raises ``HomeAssistantError``.

Fan-out: when a higher-level caller needs to target multiple devices, it
calls the service once per device. Each call gets its own ``id`` and
``Future``, so responses are correlated independently.

Device state: the app pushes a full state snapshot every 60s (heartbeat)
and on any value change via ``update_managed_device_state``. If 3
consecutive heartbeats are missed (180s), the device is marked offline
with ``offline_reason`` ``timeout`` and all its entities become
unavailable. ``unregister_managed_device`` marks it offline with
``unregistered``.

Persistence: each record's metadata, ``offline_reason``, ``last_seen`` and
the optional entities it has (not its runtime state) are stored in the
config entry so entities survive HA restarts. ``last_seen`` is also written
when HA stops, so it keeps the last heartbeat across a restart. On boot,
entities are restored as unavailable until the device reconnects and
re-registers.

Optional entities: a property marked ``optional`` is a sensor only some
devices have, such as a wall panel's presence sensor. A record gets its
entity once a state update carries a value other than null for it, and keeps
it from then on.

Status events: ``bngnt_managed_device_updated`` carries a record exactly as
``list_managed_devices`` lists it, admin code status included, and also fires
when a device's admin code report changes that status.
``bngnt_managed_device_removed`` carries the ``managed_device_id`` of a record
that's gone. The app listens to them while it shows device status.
``bngnt_managed_device_replaced`` carries the ``managed_device_id`` of a
record a replace gave to another device, and that device's ids: the device
listens for its own.

Replace: the request carries the ids of the replacement, the device that
takes the record over. It can come from another device, such as an admin's
phone. The replacement's own record is the one it leaves behind.

Admin codes: ``admin_code.py`` keeps the admin code each record's device last
reported, by managed device ID. Deleting a record removes its entry. A replace
leaves the target's entry as it is, or the target without one, and drops the
replacement's: the old device's code state wins. A code never goes in a
record, its serialisation or an event: only its status does.

Settings backups: ``settings_backup.py`` keeps each record's latest settings
backup, by managed device ID, and the restore a replace leaves on the target:
a snapshot of the target's backup and admin code, for the replacement to
fetch. Deleting a record removes its entry. None of it goes in a record, its
serialisation or an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import voluptuous as vol

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_MANAGED_DEVICES, DOMAIN, SERVICE_DEVICE_IDENTIFIER

if TYPE_CHECKING:
    from .admin_code import BNGntAdminCodeStore
    from .settings_backup import BNGntSettingsBackupStore

_LOGGER = logging.getLogger(__name__)

OFFLINE_TIMEOUT = 180
COMMAND_TIMEOUT = 10

MANAGED_DEVICE_ID_MAX_LENGTH = 255

# Expected failures, returned in the service response as ``error`` rather
# than raised. The app matches these strings, and the Homey app returns the
# same ones, so keep all three in sync.
ERROR_REPLACEMENT_NOT_REGISTERED = "replacement_not_registered"
ERROR_TARGET_NOT_FOUND = "target_not_found"
ERROR_TARGET_ONLINE = "target_online"

# Why a record is offline. A plain string on the wire, so a hub can add a
# reason without an app release. The registry only writes these, and the app
# and the Homey app know the same ones, so keep all three in sync.
OFFLINE_REASON_TIMEOUT = "timeout"
OFFLINE_REASON_UNREGISTERED = "unregistered"
OFFLINE_REASONS = (OFFLINE_REASON_TIMEOUT, OFFLINE_REASON_UNREGISTERED)

OfflineReason = Literal["timeout", "unregistered"]

# The app subscribes to these by name, and the Homey app emits the same ones.
EVENT_MANAGED_DEVICE_UPDATED = "bngnt_managed_device_updated"
EVENT_MANAGED_DEVICE_REMOVED = "bngnt_managed_device_removed"
EVENT_MANAGED_DEVICE_REPLACED = "bngnt_managed_device_replaced"

# JSON-RPC 2.0's code for a method the device doesn't know: its app predates
# the command.
JSONRPC_METHOD_NOT_FOUND = -32601

# The values of the on/off settings and of wake on approach's sensitivity, in
# both the state the app reports and the commands it takes.
ON_OFF = ["on", "off"]
WAKE_ON_APPROACH_SENSITIVITIES = ["very_close", "close", "medium", "far"]

# What a replace copies from the replacement's record into the target's. Not
# ``optional_entities``: those are entities of the target's HA device, which
# the target keeps.
_ATTACHED_DEVICE_FIELDS = (
    "profile_id",
    "device_id",
    "device_name",
    "device_brand",
    "device_type",
    "device_model_name",
    "device_os",
    "device_os_version",
    "app_version",
    "state",
    "online",
    "offline_reason",
    "last_seen",
)


# ---------------------------------------------------------------------------
# Sensor / binary-sensor property definitions
#
# state_key: the key inside the decoded state dict (snake_case, since the
# HA client in the Zendo app converts camelCase -> snake_case before
# sending the state JSON string).
#
# translation_key: takes the entity's name, state labels and icons from
# strings.json and icons.json instead of ``name`` and ``icon``.
#
# optional: the entity is created only once the device reports a value other
# than null for it (see the module docstring).
# ---------------------------------------------------------------------------

SENSOR_PROPERTIES: dict[str, dict] = {
    "battery_level": {
        "name": "Battery",
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "native_unit": "%",
        "icon": None,
        "state_key": "battery_level",
    },
    "battery_state": {
        "name": "Battery state",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "icon": "mdi:battery-charging",
        "state_key": "battery_state",
        "options": ["charger_unplugged", "charging", "plugged_not_charging"],
    },
    "screen_brightness": {
        "name": "Screen brightness",
        "device_class": None,
        "state_class": None,
        "native_unit": "%",
        "icon": "mdi:brightness-6",
        "state_key": "screen_brightness",
    },
    "volume": {
        "name": "Volume",
        "device_class": None,
        "state_class": None,
        "native_unit": "%",
        "icon": "mdi:volume-high",
        "state_key": "volume",
    },
    "screen_state": {
        "name": "Screen",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "icon": "mdi:monitor",
        "state_key": "screen_state",
        "options": ["active", "screensaver"],
    },
    "color_scheme": {
        "name": "Color scheme",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "icon": "mdi:theme-light-dark",
        "state_key": "color_scheme",
        "options": ["dark", "light"],
    },
    "screensaver_mode": {
        "name": "Screensaver mode",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "icon": "mdi:weather-night",
        "state_key": "screensaver_mode",
        "options": ["none", "dim", "black", "clock"],
    },
    "screensaver_timer": {
        "name": "Screensaver timer",
        "device_class": None,
        "state_class": None,
        "native_unit": "min",
        "icon": "mdi:timer-outline",
        "state_key": "screensaver_timer",
    },
    # null: the device has no working light sensor.
    "auto_brightness": {
        "translation_key": "auto_brightness",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "state_key": "auto_brightness",
        "options": ON_OFF,
        "optional": True,
    },
    # null: the device has no working presence or proximity sensor.
    "wake_on_approach": {
        "translation_key": "wake_on_approach",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "state_key": "wake_on_approach",
        "options": ON_OFF,
        "optional": True,
    },
    # null: the device's wake on approach has no sensitivity levels.
    "wake_on_approach_sensitivity": {
        "translation_key": "wake_on_approach_sensitivity",
        "device_class": SensorDeviceClass.ENUM,
        "state_class": None,
        "native_unit": None,
        "state_key": "wake_on_approach_sensitivity",
        "options": WAKE_ON_APPROACH_SENSITIVITIES,
        "optional": True,
    },
}

BINARY_SENSOR_PROPERTIES: dict[str, dict] = {
    "locked": {
        "name": "Locked",
        "device_class": None,
        "icon": None,
        "state_key": "locked",
    },
    # null: no sensor, or no reading yet.
    "presence": {
        "translation_key": "presence",
        # Occupancy reads Detected / Clear. The presence device class reads
        # Home / Away, which is wrong at a commercial property.
        "device_class": BinarySensorDeviceClass.OCCUPANCY,
        "state_key": "presence",
        # The reported value for on and off. Anything else is unknown.
        "values": {"present": True, "absent": False},
        "optional": True,
    },
}


# ---------------------------------------------------------------------------
# Service schemas -- device-facing (called by the app)
#
# All fields are snake_case per HA convention. The Zendo app's HA client
# converts camelCase -> snake_case when calling services.
# extra=REMOVE_EXTRA for forward compatibility when new fields are added.
# ---------------------------------------------------------------------------

def _managed_device_id_hint(value: Any) -> str | None:
    # A hint the registry can't use is ignored, never an error, so a value
    # of the wrong type counts as no hint instead of failing the call.
    return value if isinstance(value, str) else None


SERVICE_REGISTER_MANAGED_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_name"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_brand"): vol.Any(None, cv.string),
        vol.Required("device_type"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_model_name"): vol.Any(None, cv.string),
        vol.Required("device_os"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("device_os_version"): vol.Any(None, cv.string),
        vol.Required("app_version"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("managed_device_id"): _managed_device_id_hint,
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_UNREGISTER_MANAGED_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_MANAGED_DEVICE_COMMAND_RESPONSE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("response"): vol.All(cv.string, vol.Length(min=1)),
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_UPDATE_MANAGED_DEVICE_STATE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("state"): vol.All(cv.string, vol.Length(min=1)),
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_LIST_MANAGED_DEVICES_SCHEMA = vol.Schema({}, extra=vol.REMOVE_EXTRA)

SERVICE_REPLACE_MANAGED_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("managed_device_id"): vol.All(cv.string, vol.Length(min=1)),
    },
    extra=vol.REMOVE_EXTRA,
)

SERVICE_DELETE_MANAGED_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("managed_device_id"): vol.All(cv.string, vol.Length(min=1)),
    },
    extra=vol.REMOVE_EXTRA,
)


# ---------------------------------------------------------------------------
# Service schemas -- automation-facing (called by HA automations)
# ---------------------------------------------------------------------------

def _validate_pin(value: str) -> str:
    if not isinstance(value, str) or not value.isdigit() or len(value) not in (4, 6, 8):
        raise vol.Invalid("PIN must be exactly 4, 6, or 8 digits")
    return value


_DEVICE_VALIDATOR = vol.Any(cv.string, [cv.string])

SERVICE_MANAGED_DEVICE_SCREENSAVER_CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required("device"): _DEVICE_VALIDATOR,
        vol.Optional("mode"): vol.In(["none", "dim", "black", "clock"]),
        vol.Optional("timer"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
    },
)

SERVICE_MANAGED_DEVICE_SCREEN_CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required("device"): _DEVICE_VALIDATOR,
        vol.Optional("brightness"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional("color_scheme"): vol.In(["dark", "light"]),
    },
)

SERVICE_MANAGED_DEVICE_SCREEN_WAKE_UP_SCHEMA = vol.Schema(
    {vol.Required("device"): _DEVICE_VALIDATOR},
)

SERVICE_MANAGED_DEVICE_AUDIO_CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required("device"): _DEVICE_VALIDATOR,
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    },
)

SERVICE_MANAGED_DEVICE_APP_RELOAD_SCHEMA = vol.Schema(
    {vol.Required("device"): _DEVICE_VALIDATOR},
)

SERVICE_MANAGED_DEVICE_APP_LOCK_SCHEMA = vol.Schema(
    {
        vol.Required("device"): _DEVICE_VALIDATOR,
        vol.Required("pin"): _validate_pin,
        vol.Optional("message"): cv.string,
    },
)

SERVICE_MANAGED_DEVICE_APP_UNLOCK_SCHEMA = vol.Schema(
    {vol.Required("device"): _DEVICE_VALIDATOR},
)


def _on_off_from_bool(value: Any) -> Any:
    # YAML 1.1 reads an unquoted on / off as a boolean.
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


_ON_OFF_VALIDATOR = vol.All(_on_off_from_bool, vol.In(ON_OFF))

SERVICE_MANAGED_DEVICE_AUTO_BRIGHTNESS_CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required("device"): _DEVICE_VALIDATOR,
        vol.Required("mode"): _ON_OFF_VALIDATOR,
    },
)

SERVICE_MANAGED_DEVICE_WAKE_ON_APPROACH_CONFIGURE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("device"): _DEVICE_VALIDATOR,
            vol.Optional("mode"): _ON_OFF_VALIDATOR,
            vol.Optional("sensitivity"): vol.In(WAKE_ON_APPROACH_SENSITIVITIES),
        },
    ),
    cv.has_at_least_one_key("mode", "sensitivity"),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class BNGntManagedDeviceError(HomeAssistantError):
    """An expected failure, which the service returns to the app as ``error``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BNGntManagedDeviceCommandError(HomeAssistantError):
    """A JSON-RPC error object a device answered a command with."""

    def __init__(self, message: str, code: Any) -> None:
        super().__init__(f"Device returned error: {message}")
        self.code = code


class BNGntManagedDeviceTimeoutError(HomeAssistantError):
    """A device didn't answer a command in time. It may still have run it."""


def _build_model(device_brand: str | None, device_model_name: str | None) -> str | None:
    parts = [p for p in [device_brand, device_model_name] if p]
    return " ".join(parts) if parts else None


def _is_valid_hint(hint: str | None) -> bool:
    return (
        isinstance(hint, str)
        and 0 < len(hint) <= MANAGED_DEVICE_ID_MAX_LENGTH
        and hint != SERVICE_DEVICE_IDENTIFIER
    )


def _epoch_ms(timestamp: float | None) -> int | None:
    return int(timestamp * 1000) if timestamp else None


class BNGntManagedDeviceRecord(TypedDict):
    """A managed device's record, keyed by its managed device ID."""

    profile_id: str
    device_id: str
    device_name: str
    device_brand: str | None
    device_type: str
    device_model_name: str | None
    device_os: str
    device_os_version: str | None
    app_version: str | None
    state: dict[str, Any]
    online: bool
    offline_reason: OfflineReason | None
    # Unix seconds, or None for a record restored from before 1.9.0 whose
    # device hasn't been seen since.
    last_seen: float | None
    ha_device_id: str | None
    # The property keys of the optional entities created for this record.
    optional_entities: list[str]


def _serialize_managed_device(
    hass: HomeAssistant,
    key: str,
    device: BNGntManagedDeviceRecord,
    admin_code_status: str,
) -> dict[str, Any]:
    """Return a record as ``list_managed_devices`` lists it.

    ``bngnt_managed_device_updated`` carries exactly this, so the app parses
    both with the same schema. ``admin_code_status`` comes from the admin code
    store: the status only, never the code.
    """
    ha_device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, key)})
    return {
        "managed_device_id": key,
        "profile_id": device["profile_id"],
        "device_id": device["device_id"],
        "device_name": device["device_name"],
        "name_by_user": ha_device.name_by_user if ha_device else None,
        "device_brand": device.get("device_brand"),
        "device_type": device.get("device_type"),
        "device_model_name": device.get("device_model_name"),
        "device_os": device.get("device_os"),
        "device_os_version": device.get("device_os_version"),
        "app_version": device.get("app_version"),
        "online": device["online"],
        "offline_reason": None if device["online"] else device.get("offline_reason"),
        "last_seen": _epoch_ms(device.get("last_seen")),
        "state": device["state"] or None,
        "admin_code_status": admin_code_status,
    }


def _without_wifi_signal(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "wifi_signal"}


def _restored_optional_entities(value: Any) -> list[str]:
    # A key this version doesn't know is kept, so a newer version that wrote
    # it gets its entity back after a downgrade and upgrade.
    if not isinstance(value, list):
        return []
    return [prop_key for prop_key in value if isinstance(prop_key, str)]


class BNGntManagedDeviceRegistry:
    """Tracks managed devices, the device attached to each, and online status.

    ``_devices`` maps each managed device ID to its record. ``admin_codes``
    keeps the admin code each record's device last reported, and
    ``settings_backups`` each record's settings backup and restore, by the
    same ID.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        admin_codes: BNGntAdminCodeStore,
        settings_backups: BNGntSettingsBackupStore,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._admin_codes = admin_codes
        self._settings_backups = settings_backups
        self._devices: dict[str, BNGntManagedDeviceRecord] = {}
        self._pending_commands: dict[str, asyncio.Future] = {}
        self._offline_check_unsub: callable | None = None
        self._ha_stop_unsub: CALLBACK_TYPE | None = None

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        """Restore persisted devices and start the offline checker."""
        persisted: dict = self._entry.data.get(CONF_MANAGED_DEVICES, {})
        for key, info in persisted.items():
            self._restore_device(key, info)

        self._offline_check_unsub = async_track_time_interval(
            self._hass, self._check_offline, timedelta(seconds=60)
        )
        self._ha_stop_unsub = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._persist_on_ha_stop
        )

    def stop(self) -> None:
        """Stop the offline checker, persist, and cancel pending commands."""
        if self._offline_check_unsub:
            self._offline_check_unsub()
            self._offline_check_unsub = None

        if self._ha_stop_unsub:
            self._ha_stop_unsub()
            self._ha_stop_unsub = None

        # A reload restores each record from the config entry, as a restart
        # does, so last_seen is written here as well as on HA stop.
        self._persist_devices()

        for future in self._pending_commands.values():
            if not future.done():
                future.cancel()
        self._pending_commands.clear()

    @callback
    def _persist_on_ha_stop(self, _event: Event) -> None:
        # Removing a once-listener after it has fired logs an error.
        self._ha_stop_unsub = None
        # Heartbeats update last_seen only in memory. The config entry store
        # writes this at final write, after every stop listener has run.
        self._persist_devices()

    # -- public API ----------------------------------------------------------

    @property
    def devices(self) -> dict[str, BNGntManagedDeviceRecord]:
        return self._devices

    @property
    def admin_codes(self) -> BNGntAdminCodeStore:
        return self._admin_codes

    @property
    def settings_backups(self) -> BNGntSettingsBackupStore:
        return self._settings_backups

    def find_key(self, profile_id: str, device_id: str) -> str | None:
        """Return the managed device ID the device is attached to, if any."""
        return self._find_key(profile_id, device_id)

    def serialize(self, key: str) -> dict[str, Any]:
        """Return the record as ``list_managed_devices`` lists it."""
        return _serialize_managed_device(
            self._hass, key, self._devices[key], self._admin_codes.status(key)
        )

    @callback
    def report_admin_code(self, key: str, code: str | None) -> None:
        """Keep the admin code the record's device reported, or None for none.

        Fires ``bngnt_managed_device_updated`` when the record's admin code
        status changes. A new code with the status still set fires nothing.
        """
        status_before = self._admin_codes.status(key)
        self._admin_codes.report(key, code)
        if self._admin_codes.status(key) != status_before:
            self._fire_updated(key)

    def get_device(self, key: str) -> BNGntManagedDeviceRecord | None:
        return self._devices.get(key)

    def list_devices(self) -> list[tuple[str, BNGntManagedDeviceRecord]]:
        """Return each managed device ID with its record."""
        return list(self._devices.items())

    def register_device(
        self,
        profile_id: str,
        device_id: str,
        device_name: str,
        device_brand: str | None,
        device_type: str,
        device_model_name: str | None,
        device_os: str,
        device_os_version: str | None,
        app_version: str,
        managed_device_id: str | None = None,
    ) -> str:
        """Register or update the device's managed device. Returns its key.

        Called on kiosk-mode enable AND on every control-backend connect
        (idempotent). The record is found by the attached ids, and
        ``managed_device_id`` is only a hint for when none is attached. On
        first call: creates the HA device + sensor entities. On subsequent
        calls: updates device metadata (name, model, version) and marks it
        back online if it was offline.
        """
        key = self._find_key(profile_id, device_id)
        is_new = key is None
        if key is None:
            key = self._choose_new_key(profile_id, device_id, managed_device_id)

        attached = {
            "profile_id": profile_id,
            "device_id": device_id,
            "device_name": device_name,
            "device_brand": device_brand,
            "device_type": device_type,
            "device_model_name": device_model_name,
            "device_os": device_os,
            "device_os_version": device_os_version,
            "app_version": app_version,
            "online": True,
            "offline_reason": None,
            "last_seen": time.time(),
        }

        if is_new:
            self._devices[key] = {
                **attached,
                "state": {},
                "ha_device_id": None,
                "optional_entities": [],
            }
        else:
            self._devices[key].update(attached)

        # The key must be settled before this: an identifier built from the
        # attached ids would add a stray HA device for any record whose key
        # is something else. A key that matches a deleted HA device restores
        # that device, and its entities' registry entries come back with it.
        self._sync_ha_device(key)

        if is_new:
            self._create_entities(key, available=True)
        else:
            self._set_entities_available(key, True)

        self._persist_devices()
        self._fire_updated(key)
        return key

    def unregister_device(self, profile_id: str, device_id: str) -> bool:
        """Mark the device's managed device unavailable (intentional disconnect).

        Called when kiosk mode is disabled. The record, its HA device and its
        entities stay (not deleted) so automations referencing them don't
        break -- they just show as unavailable. So do its admin code and
        settings backup entries.
        Returns False when no managed device is attached to the device.
        """
        key = self._find_key(profile_id, device_id)
        if key is None:
            return False

        device = self._devices[key]
        # A record that already timed out still takes the reason, so the app
        # shows it as no longer monitored rather than as lost.
        changed = (
            device["online"]
            or device.get("offline_reason") != OFFLINE_REASON_UNREGISTERED
        )
        if device["online"]:
            device["online"] = False
            device["last_seen"] = time.time()
            self._set_entities_available(key, False)
        device["offline_reason"] = OFFLINE_REASON_UNREGISTERED

        if changed:
            self._persist_devices()
            self._fire_updated(key)
        return True

    def update_state(
        self, profile_id: str, device_id: str, state: dict
    ) -> str | None:
        """Update device state from a heartbeat or state-change push.

        Returns the record's key, or None when no managed device is attached
        to the device, which tells the app to register again.
        """
        key = self._find_key(profile_id, device_id)
        if key is None:
            _LOGGER.debug(
                "State update for unknown managed device: profile %s, device %s",
                profile_id,
                device_id,
            )
            return None

        device = self._devices[key]
        was_offline = not device["online"]
        old_state = device["state"]
        device["state"] = state
        device["last_seen"] = time.time()
        device["online"] = True
        device["offline_reason"] = None

        if "app_version" in state and state["app_version"] != device.get("app_version"):
            device["app_version"] = state["app_version"]
            dev_reg = dr.async_get(self._hass)
            if device.get("ha_device_id"):
                dev_reg.async_update_device(
                    device["ha_device_id"], sw_version=state["app_version"]
                )

        if was_offline:
            self._set_entities_available(key, True)

        created = self._create_reported_optional_entities(key)
        if was_offline or created:
            self._persist_devices()

        self._update_entity_values(key)

        # The Wi-Fi signal drifts from one heartbeat to the next, so a change
        # in it alone fires no event: the app reads it from the list. The
        # Homey app follows the same rule.
        if (
            was_offline
            or not old_state
            or _without_wifi_signal(old_state) != _without_wifi_signal(state)
        ):
            self._fire_updated(key)
        return key

    def replace_device(
        self, profile_id: str, device_id: str, managed_device_id: str
    ) -> tuple[str, str]:
        """Attach the replacement to the managed device ``managed_device_id``.

        The replacement is the device whose ids are given, wherever the
        request comes from. The target keeps its HA device, entity IDs, area
        and the name the user gave it, and takes the replacement's attached
        ids, device info, state and online status.

        The target keeps its own admin code entry, or stays without one, and
        never takes the replacement's. It gets a settings restore for the
        replacement to fetch: a snapshot of its latest backup and of its admin
        code status and code, or the data of a restore still pending on it,
        under a new ID. The record the replacement
        leaves behind, its HA device, entities and entries are removed.
        Returns the target's key and its device name from before.

        Raises ``BNGntManagedDeviceError`` with ``replacement_not_registered``
        or ``target_not_found``.
        """
        # Keep this synchronous: no other service call may run between
        # finding the two records and changing them.
        replacement_key = self._find_key(profile_id, device_id)
        if replacement_key is None:
            raise BNGntManagedDeviceError(ERROR_REPLACEMENT_NOT_REGISTERED)

        target = self._devices.get(managed_device_id)
        if target is None:
            raise BNGntManagedDeviceError(ERROR_TARGET_NOT_FOUND)

        # Already attached: a retry after a lost response.
        if replacement_key == managed_device_id:
            return managed_device_id, target["device_name"]

        # Read before anything changes: the snapshot is of the device that
        # was attached to the target.
        previous_device_name = target["device_name"]
        admin_code_entry = self._admin_codes.get(managed_device_id)
        previous_admin_code_status = self._admin_codes.status(managed_device_id)
        previous_admin_code = (
            admin_code_entry["code"] if admin_code_entry is not None else None
        )

        replacement = self._devices[replacement_key]
        target.update(
            {field: replacement.get(field) for field in _ATTACHED_DEVICE_FIELDS}
        )
        self._sync_ha_device(managed_device_id)
        self._admin_codes.keep_on_replace(replacement_key, managed_device_id)
        self._settings_backups.on_replace(
            replacement_key,
            managed_device_id,
            previous_device_name,
            previous_admin_code_status,
            previous_admin_code,
        )

        self._forget_device(replacement_key)
        self._remove_ha_device(replacement_key)

        self._set_entities_available(managed_device_id, target["online"])
        self._update_entity_values(managed_device_id)
        self._create_reported_optional_entities(managed_device_id)
        self._persist_devices()
        self._fire_removed(replacement_key)
        self._fire_updated(managed_device_id)
        self._fire_replaced(managed_device_id)
        return managed_device_id, previous_device_name

    def delete_device(self, key: str, *, remove_ha_device: bool = True) -> bool:
        """Forget an offline managed device and its admin code and backup entries.

        Returns False if there is none. Pass ``remove_ha_device=False`` from
        HA's Delete button, where HA removes the device and its entities itself
        once this returns.

        Raises ``BNGntManagedDeviceError`` with ``target_online``.
        """
        device = self._devices.get(key)
        if device is None:
            return False
        if device["online"]:
            raise BNGntManagedDeviceError(ERROR_TARGET_ONLINE)

        self._forget_device(key)
        self._admin_codes.remove(key)
        self._settings_backups.remove(key)
        if remove_ha_device:
            self._remove_ha_device(key)
        self._persist_devices()
        self._fire_removed(key)
        return True

    async def send_command(
        self, key: str, method: str, params: dict | None = None
    ) -> dict:
        """Fire a JSON-RPC command event and wait for the device response.

        The round-trip works via the HA event bus:
        1. Fire ``bngnt_managed_device_command`` with a JSON-stringified
           JSON-RPC request and the attached ``profile_id`` + ``device_id``.
        2. The Zendo app receives it via WebSocket, processes the RPC,
           and calls ``zendo.managed_device_command_response`` with the
           JSON-RPC response (same ``id``).
        3. ``handle_command_response`` resolves the Future stored here,
           unblocking this coroutine.

        Raises ``HomeAssistantError``: ``BNGntManagedDeviceTimeoutError`` on
        timeout, and ``BNGntManagedDeviceCommandError`` if the device returns
        a JSON-RPC error object. Method not found means the app on the device
        predates the command, and gets a message that says so instead.
        """
        device = self._devices.get(key)
        if not device:
            raise HomeAssistantError(f"Unknown managed device: {key}")
        if not device["online"]:
            raise HomeAssistantError(
                f"Device '{device['device_name']}' is offline"
            )

        rpc_id = str(uuid.uuid4())
        rpc_request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": rpc_id,
        }

        future: asyncio.Future = self._hass.loop.create_future()
        self._pending_commands[rpc_id] = future

        self._hass.bus.async_fire(
            "bngnt_managed_device_command",
            {
                "profile_id": device["profile_id"],
                "device_id": device["device_id"],
                "request": json.dumps(rpc_request),
            },
        )

        try:
            return await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            raise BNGntManagedDeviceTimeoutError(
                f"Device '{device['device_name']}' did not respond "
                f"within {COMMAND_TIMEOUT} seconds"
            ) from None
        except BNGntManagedDeviceCommandError as err:
            if err.code == JSONRPC_METHOD_NOT_FOUND:
                raise HomeAssistantError(
                    f"Device '{device['device_name']}' needs a newer version "
                    "of the Zendo app"
                ) from err
            raise
        finally:
            self._pending_commands.pop(rpc_id, None)

    def handle_command_response(self, response: dict) -> None:
        """Correlate a JSON-RPC response by its ``id`` and resolve the future.

        Called by the ``managed_device_command_response`` service handler.
        The response is a parsed JSON-RPC 2.0 response dict. The ``id``
        field must match a pending ``send_command`` call. Late responses
        (after timeout) are silently dropped.
        """
        rpc_id = response.get("id")
        if not rpc_id:
            _LOGGER.warning("Command response missing 'id'")
            return

        future = self._pending_commands.get(rpc_id)
        if not future or future.done():
            _LOGGER.debug(
                "No pending command for id %s (may have timed out)", rpc_id
            )
            return

        if "error" in response:
            error = response["error"]
            msg = (
                error.get("message", "Unknown error")
                if isinstance(error, dict)
                else str(error)
            )
            code = error.get("code") if isinstance(error, dict) else None
            future.set_exception(BNGntManagedDeviceCommandError(msg, code))
        else:
            future.set_result(response.get("result", {}))

    def resolve_ha_device_to_key(self, ha_device_id: str) -> str | None:
        """Resolve an HA device-registry ID to a managed device ID."""
        dev_reg = dr.async_get(self._hass)
        device_entry = dev_reg.async_get(ha_device_id)
        if not device_entry:
            return None
        for domain, identifier in device_entry.identifiers:
            if domain == DOMAIN and identifier in self._devices:
                return identifier
        return None

    # -- internal ------------------------------------------------------------

    def _find_key(self, profile_id: str, device_id: str) -> str | None:
        keys = [
            key
            for key, device in self._devices.items()
            if device["profile_id"] == profile_id and device["device_id"] == device_id
        ]
        if len(keys) > 1:
            _LOGGER.warning(
                "Profile %s, device %s is attached to managed devices %s; using %s",
                profile_id,
                device_id,
                keys,
                keys[0],
            )
        return keys[0] if keys else None

    def _choose_new_key(
        self, profile_id: str, device_id: str, hint: str | None
    ) -> str:
        if _is_valid_hint(hint) and hint not in self._devices:
            return hint

        # Reuses any HA device and entities already made for these ids,
        # including deleted ones.
        legacy_key = f"{profile_id}_{device_id}"
        if legacy_key not in self._devices:
            return legacy_key

        return f"md_{uuid.uuid4().hex}"

    def _sync_ha_device(self, key: str) -> None:
        """Create or update a record's HA device from the record.

        Leaves the name the user gave it and its area alone.
        """
        device = self._devices[key]
        model = _build_model(device.get("device_brand"), device.get("device_model_name"))

        dev_reg = dr.async_get(self._hass)
        ha_device = dev_reg.async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={(DOMAIN, key)},
            name=device["device_name"],
            manufacturer="Zendo",
            model=model,
            sw_version=device.get("app_version"),
        )
        dev_reg.async_update_device(
            ha_device.id,
            name=device["device_name"],
            model=model,
            sw_version=device.get("app_version"),
        )
        device["ha_device_id"] = ha_device.id

    def _forget_device(self, key: str) -> None:
        self._devices.pop(key, None)
        self._hass.data[DOMAIN].get("managed_device_entities", {}).pop(key, None)

    def _remove_ha_device(self, key: str) -> None:
        # With no other config entry left on it, HA removes the device and
        # its entities.
        dev_reg = dr.async_get(self._hass)
        ha_device = dev_reg.async_get_device(identifiers={(DOMAIN, key)})
        if ha_device is not None:
            dev_reg.async_update_device(
                ha_device.id, remove_config_entry_id=self._entry.entry_id
            )

    def _fire_updated(self, key: str) -> None:
        self._hass.bus.async_fire(EVENT_MANAGED_DEVICE_UPDATED, self.serialize(key))

    def _fire_removed(self, key: str) -> None:
        self._hass.bus.async_fire(
            EVENT_MANAGED_DEVICE_REMOVED, {"managed_device_id": key}
        )

    def _fire_replaced(self, key: str) -> None:
        device = self._devices[key]
        self._hass.bus.async_fire(
            EVENT_MANAGED_DEVICE_REPLACED,
            {
                "managed_device_id": key,
                "profile_id": device["profile_id"],
                "device_id": device["device_id"],
            },
        )

    @callback
    def _check_offline(self, _now) -> None:
        threshold = time.time() - OFFLINE_TIMEOUT
        went_offline: list[str] = []
        for key, device in list(self._devices.items()):
            if device["online"] and device["last_seen"] < threshold:
                _LOGGER.info("Managed device %s went offline", key)
                # last_seen stays at the last heartbeat.
                device["online"] = False
                device["offline_reason"] = OFFLINE_REASON_TIMEOUT
                self._set_entities_available(key, False)
                went_offline.append(key)

        if went_offline:
            self._persist_devices()
            for key in went_offline:
                self._fire_updated(key)

    def _restore_device(self, key: str, info: dict) -> None:
        offline_reason = info.get("offline_reason")
        self._devices[key] = {
            **info,
            "device_os_version": info.get("device_os_version"),
            "online": False,
            # A record that was online when HA stopped has no reason, and nor
            # does one persisted by an older version: as far as HA knows, its device
            # lost contact.
            "offline_reason": (
                offline_reason
                if offline_reason in OFFLINE_REASONS
                else OFFLINE_REASON_TIMEOUT
            ),
            "last_seen": info.get("last_seen"),
            "state": {},
            "ha_device_id": None,
            "optional_entities": _restored_optional_entities(
                info.get("optional_entities")
            ),
        }
        self._sync_ha_device(key)
        self._create_entities(key, available=False)

    def _create_entities(self, key: str, available: bool) -> None:
        """Create a record's entities, with the optional ones it already has."""
        created = self._devices[key]["optional_entities"]

        def wanted(properties: dict[str, dict]) -> list[str]:
            return [
                prop_key
                for prop_key, prop in properties.items()
                if not prop.get("optional") or prop_key in created
            ]

        self._add_entities(
            key,
            wanted(SENSOR_PROPERTIES),
            wanted(BINARY_SENSOR_PROPERTIES),
            available,
        )

    def _create_reported_optional_entities(self, key: str) -> bool:
        """Create the optional entities the record's state now has a value for.

        Returns True if it created any. The record lists them straight away,
        so a second state update before HA has added them doesn't add them
        again.
        """
        device = self._devices[key]
        state = device["state"]
        created = device["optional_entities"]

        def reported(properties: dict[str, dict]) -> list[str]:
            return [
                prop_key
                for prop_key, prop in properties.items()
                if prop.get("optional")
                and prop_key not in created
                and state.get(prop["state_key"]) is not None
            ]

        sensor_keys = reported(SENSOR_PROPERTIES)
        binary_sensor_keys = reported(BINARY_SENSOR_PROPERTIES)
        if not sensor_keys and not binary_sensor_keys:
            return False

        created.extend(sensor_keys + binary_sensor_keys)
        self._add_entities(key, sensor_keys, binary_sensor_keys, device["online"])
        return True

    def _add_entities(
        self,
        key: str,
        sensor_keys: list[str],
        binary_sensor_keys: list[str],
        available: bool,
    ) -> None:
        device = self._devices[key]

        add_sensors = self._hass.data[DOMAIN].get(
            "async_add_managed_device_sensors"
        )
        add_binary_sensors = self._hass.data[DOMAIN].get(
            "async_add_managed_device_binary_sensors"
        )

        if add_sensors and sensor_keys:
            add_sensors(
                [
                    BNGntManagedDeviceSensor(
                        key, device, prop_key, SENSOR_PROPERTIES[prop_key], self,
                        available,
                    )
                    for prop_key in sensor_keys
                ],
                update_before_add=True,
            )

        if add_binary_sensors and binary_sensor_keys:
            add_binary_sensors(
                [
                    BNGntManagedDeviceBinarySensor(
                        key, device, prop_key, BINARY_SENSOR_PROPERTIES[prop_key],
                        self, available,
                    )
                    for prop_key in binary_sensor_keys
                ],
                update_before_add=True,
            )

    def _set_entities_available(self, key: str, available: bool) -> None:
        entities: list = (
            self._hass.data[DOMAIN]
            .get("managed_device_entities", {})
            .get(key, [])
        )
        for entity in entities:
            entity.set_device_available(available)

    def _update_entity_values(self, key: str) -> None:
        entities: list = (
            self._hass.data[DOMAIN]
            .get("managed_device_entities", {})
            .get(key, [])
        )
        for entity in entities:
            entity.notify_state_updated()

    def _persist_devices(self) -> None:
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return
        target_entry = entries[0]

        persisted: dict[str, dict] = {}
        for key, device in self._devices.items():
            persisted[key] = {
                "profile_id": device["profile_id"],
                "device_id": device["device_id"],
                "device_name": device["device_name"],
                "device_brand": device.get("device_brand"),
                "device_type": device.get("device_type", ""),
                "device_model_name": device.get("device_model_name"),
                "device_os": device.get("device_os", ""),
                "device_os_version": device.get("device_os_version"),
                "app_version": device.get("app_version"),
                "offline_reason": device.get("offline_reason"),
                "last_seen": device.get("last_seen"),
                "optional_entities": list(device.get("optional_entities", [])),
            }

        self._hass.config_entries.async_update_entry(
            target_entry,
            data={**target_entry.data, CONF_MANAGED_DEVICES: persisted},
        )


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

def _device_info_from_dict(key: str, device_info: dict) -> DeviceInfo:
    model = _build_model(
        device_info.get("device_brand"),
        device_info.get("device_model_name"),
    )
    return DeviceInfo(
        identifiers={(DOMAIN, key)},
        name=device_info["device_name"],
        manufacturer="Zendo",
        model=model,
        sw_version=device_info.get("app_version"),
    )


class BNGntManagedDeviceSensor(SensorEntity):
    """A sensor entity for a single property of a managed device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        key: str,
        device_info: dict,
        prop_key: str,
        prop_def: dict,
        registry: BNGntManagedDeviceRegistry,
        available: bool,
    ) -> None:
        self._key = key
        self._registry = registry
        self._state_key = prop_def["state_key"]
        self._device_available = available
        self._options: list[str] | None = prop_def.get("options")

        self._attr_unique_id = f"md_{key}_{prop_key}"
        if prop_def.get("translation_key"):
            self._attr_translation_key = prop_def["translation_key"]
        else:
            self._attr_name = prop_def["name"]
        if prop_def.get("device_class"):
            self._attr_device_class = prop_def["device_class"]
        if prop_def.get("state_class"):
            self._attr_state_class = prop_def["state_class"]
        if prop_def.get("native_unit"):
            self._attr_native_unit_of_measurement = prop_def["native_unit"]
        if prop_def.get("icon"):
            self._attr_icon = prop_def["icon"]
        if self._options:
            self._attr_options = self._options

        self._device_info_snapshot = _device_info_from_dict(key, device_info)

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info_snapshot

    @property
    def native_value(self):
        device = self._registry.get_device(self._key)
        if not device:
            return None
        value = device.get("state", {}).get(self._state_key)
        # The state is loose on the wire, and an enum sensor raises on a
        # value outside its options, which would fail the state update.
        if self._options and value not in self._options:
            return None
        return value

    @property
    def available(self) -> bool:
        return self._device_available

    def set_device_available(self, available: bool) -> None:
        self._device_available = available
        if self.hass:
            self.async_write_ha_state()

    def notify_state_updated(self) -> None:
        if self.hass:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        entities = self.hass.data[DOMAIN].setdefault(
            "managed_device_entities", {}
        )
        entities.setdefault(self._key, []).append(self)

    async def async_will_remove_from_hass(self) -> None:
        entities = self.hass.data[DOMAIN].get("managed_device_entities", {})
        device_entities = entities.get(self._key, [])
        if self in device_entities:
            device_entities.remove(self)


class BNGntManagedDeviceBinarySensor(BinarySensorEntity):
    """A binary sensor entity for a single property of a managed device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        key: str,
        device_info: dict,
        prop_key: str,
        prop_def: dict,
        registry: BNGntManagedDeviceRegistry,
        available: bool,
    ) -> None:
        self._key = key
        self._prop_key = prop_key
        self._registry = registry
        self._state_key = prop_def["state_key"]
        self._device_available = available
        self._values: dict[str, bool] | None = prop_def.get("values")

        self._attr_unique_id = f"md_{key}_{prop_key}"
        if prop_def.get("translation_key"):
            self._attr_translation_key = prop_def["translation_key"]
        else:
            self._attr_name = prop_def["name"]
        if prop_def.get("device_class"):
            self._attr_device_class = prop_def["device_class"]
        if prop_def.get("icon"):
            self._attr_icon = prop_def["icon"]

        self._device_info_snapshot = _device_info_from_dict(key, device_info)

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info_snapshot

    @property
    def is_on(self) -> bool | None:
        device = self._registry.get_device(self._key)
        if not device:
            return None
        value = device.get("state", {}).get(self._state_key)
        if self._values is None:
            return value
        return self._values.get(value) if isinstance(value, str) else None

    @property
    def available(self) -> bool:
        return self._device_available

    @property
    def extra_state_attributes(self) -> dict | None:
        return None

    def set_device_available(self, available: bool) -> None:
        self._device_available = available
        if self.hass:
            self.async_write_ha_state()

    def notify_state_updated(self) -> None:
        if self.hass:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        entities = self.hass.data[DOMAIN].setdefault(
            "managed_device_entities", {}
        )
        entities.setdefault(self._key, []).append(self)

    async def async_will_remove_from_hass(self) -> None:
        entities = self.hass.data[DOMAIN].get("managed_device_entities", {})
        device_entities = entities.get(self._key, [])
        if self in device_entities:
            device_entities.remove(self)


# ---------------------------------------------------------------------------
# Service handlers -- device-facing
#
# Expected failures come back in the response as ``success: false`` plus an
# ``error`` code, rather than being raised, so the app can tell them apart.
# ---------------------------------------------------------------------------

def _get_registry(hass: HomeAssistant) -> BNGntManagedDeviceRegistry:
    registry = hass.data[DOMAIN].get("managed_device_registry")
    if not registry:
        raise HomeAssistantError("Managed devices are not initialized")
    return registry


async def handle_register_managed_device(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    managed_device_id = registry.register_device(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
        device_name=call.data["device_name"],
        device_brand=call.data["device_brand"],
        device_type=call.data["device_type"],
        device_model_name=call.data["device_model_name"],
        device_os=call.data["device_os"],
        device_os_version=call.data.get("device_os_version"),
        app_version=call.data["app_version"],
        managed_device_id=call.data.get("managed_device_id"),
    )
    return {
        "success": True,
        "managed_device_id": managed_device_id,
        "settings_restore_pending": registry.settings_backups.has_restore(
            managed_device_id
        ),
    }


async def handle_unregister_managed_device(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    registered = registry.unregister_device(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
    )
    return {"success": True, "registered": registered}


async def handle_managed_device_command_response(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry = _get_registry(hass)
    raw = call.data["response"]
    try:
        response = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as err:
        _LOGGER.warning("Invalid JSON in command response: %s", err)
        return

    registry.handle_command_response(response)


async def handle_update_managed_device_state(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    raw = call.data["state"]
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as err:
        raise HomeAssistantError(f"Invalid JSON in state: {err}") from err

    if not isinstance(state, dict):
        raise HomeAssistantError("state must be a JSON object")

    key = registry.update_state(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
        state=state,
    )
    return {
        "success": True,
        "registered": key is not None,
        "settings_restore_pending": (
            key is not None and registry.settings_backups.has_restore(key)
        ),
    }


async def handle_list_managed_devices(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    return {
        "managed_devices": [
            registry.serialize(key) for key, _ in registry.list_devices()
        ]
    }


async def handle_replace_managed_device(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    try:
        managed_device_id, previous_device_name = registry.replace_device(
            profile_id=call.data["profile_id"],
            device_id=call.data["device_id"],
            managed_device_id=call.data["managed_device_id"],
        )
    except BNGntManagedDeviceError as err:
        return {"success": False, "error": err.code}

    return {
        "success": True,
        "managed_device_id": managed_device_id,
        "previous_device_name": previous_device_name,
    }


async def handle_delete_managed_device(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    try:
        deleted = registry.delete_device(call.data["managed_device_id"])
    except BNGntManagedDeviceError as err:
        return {"success": False, "error": err.code}

    return {"success": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# Service handlers -- automation-facing
#
# Each handler resolves the HA device-registry ID (from the ``device``
# field / device selector) to a managed device ID, builds JSON-RPC
# params from the service call data, and delegates to ``send_command``.
#
# Field name mapping: HA uses snake_case (``color_scheme``), the JSON-RPC
# protocol uses camelCase (``colorScheme``). Conversion happens here.
#
# Settings-style RPC methods (screensaver.set, screen.set, audio.set,
# wakeOnApproach.set) accept partial params -- only the fields present in the
# service call are included. The device leaves omitted settings unchanged.
#
# A wall panel setting goes only to the targets that have the sensor for it:
# a device whose last state reports null for the setting is skipped. One
# whose state lacks the key is still sent the command, so an older app
# answers Method not found and the error says to update it.
# ---------------------------------------------------------------------------

def _resolve_devices_for_command(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[BNGntManagedDeviceRegistry, list[str]]:
    """Resolve the ``device`` field to managed device IDs.

    The ``device`` field in services.yaml uses ``selector: device:`` with
    ``multiple: true``, so the value is a list of HA device IDs.
    """
    registry = _get_registry(hass)
    ha_device_ids = call.data.get("device", [])
    if isinstance(ha_device_ids, str):
        ha_device_ids = [ha_device_ids]

    keys: list[str] = []
    for ha_device_id in ha_device_ids:
        key = registry.resolve_ha_device_to_key(ha_device_id)
        if key:
            keys.append(key)

    if not keys:
        raise ServiceValidationError(
            "No managed devices found in the selected targets. "
            "Please select one or more Zendo tablets."
        )
    return registry, keys


async def _fan_out_command(
    registry: BNGntManagedDeviceRegistry,
    keys: list[str],
    method: str,
    params: dict | None = None,
) -> None:
    """Send a command to multiple devices in parallel."""
    await asyncio.gather(*(
        registry.send_command(key, method, params)
        for key in keys
    ))


def _reports_null(
    registry: BNGntManagedDeviceRegistry, key: str, state_key: str
) -> bool:
    device = registry.get_device(key)
    state = device["state"] if device else {}
    return state_key in state and state[state_key] is None


def _keys_supporting(
    registry: BNGntManagedDeviceRegistry,
    keys: list[str],
    state_key: str,
) -> list[str]:
    """Drop the keys whose last state reports null for ``state_key``."""
    supported: list[str] = []
    for key in keys:
        if _reports_null(registry, key, state_key):
            _LOGGER.info(
                "Skipping managed device %s, which reports %s as null: "
                "it doesn't support it",
                key,
                state_key,
            )
        else:
            supported.append(key)
    return supported


async def handle_managed_device_screensaver_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "mode" in call.data:
        params["mode"] = call.data["mode"]
    if "timer" in call.data:
        params["timer"] = call.data["timer"]

    await _fan_out_command(registry, keys, "screensaver.set", params)


async def handle_managed_device_screen_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "brightness" in call.data:
        params["brightness"] = call.data["brightness"]
    if "color_scheme" in call.data:
        params["colorScheme"] = call.data["color_scheme"]

    await _fan_out_command(registry, keys, "screen.set", params)


async def handle_managed_device_auto_brightness_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    keys = _keys_supporting(registry, keys, "auto_brightness")
    if not keys:
        raise ServiceValidationError(
            "The selected devices don't support auto-brightness."
        )

    await _fan_out_command(
        registry, keys, "autoBrightness.set", {"mode": call.data["mode"]}
    )


async def handle_managed_device_wake_on_approach_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    params_by_key: dict[str, dict] = {}
    for key in _keys_supporting(registry, keys, "wake_on_approach"):
        params: dict = {}
        if "mode" in call.data:
            params["mode"] = call.data["mode"]
        # Dropped for a device without levels, rather than skipping the
        # device, so a call that also sets the mode still sets it there.
        if "sensitivity" in call.data:
            if _reports_null(registry, key, "wake_on_approach_sensitivity"):
                _LOGGER.info(
                    "Not setting the sensitivity of managed device %s, "
                    "which has no sensitivity levels",
                    key,
                )
            else:
                params["sensitivity"] = call.data["sensitivity"]
        if params:
            params_by_key[key] = params

    if not params_by_key:
        if "mode" in call.data:
            raise ServiceValidationError(
                "The selected devices don't support wake on approach."
            )
        raise ServiceValidationError(
            "The selected devices don't have wake on approach sensitivity levels."
        )

    await asyncio.gather(*(
        registry.send_command(key, "wakeOnApproach.set", params)
        for key, params in params_by_key.items()
    ))


async def handle_managed_device_screen_wake_up(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, keys, "screen.wakeUp")


async def handle_managed_device_audio_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "volume" in call.data:
        params["volume"] = call.data["volume"]

    await _fan_out_command(registry, keys, "audio.set", params)


async def handle_managed_device_app_reload(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, keys, "app.reload")


async def handle_managed_device_app_lock(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)

    params: dict = {"pin": call.data["pin"]}
    if "message" in call.data:
        params["message"] = call.data["message"]

    await _fan_out_command(registry, keys, "app.lock", params)


async def handle_managed_device_app_unlock(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, keys = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, keys, "app.unlock")
