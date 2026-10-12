"""Managed device registry, entity classes, and service handlers.

Command/response flow (JSON-RPC 2.0 over the HA event bus):

1. An automation-facing service (e.g. ``managed_device_screen_wake_up``)
   builds a JSON-RPC request with a unique ``id`` (UUID).
2. The request is wrapped in a ``bngnt_managed_device_command`` bus event
   together with the target ``profile_id`` and ``device_id``.  The
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
and all its entities become unavailable.

Persistence: registered device metadata (not runtime state) is stored in
the config entry so entities survive HA restarts. On boot, entities are
restored as unavailable until the device reconnects and re-registers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta

import voluptuous as vol

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_MANAGED_DEVICES, DOMAIN

_LOGGER = logging.getLogger(__name__)

OFFLINE_TIMEOUT = 180
COMMAND_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Sensor / binary-sensor property definitions
#
# state_key: the key inside the decoded state dict (snake_case, since the
# HA client in the Zendo app converts camelCase -> snake_case before
# sending the state JSON string).
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
}

BINARY_SENSOR_PROPERTIES: dict[str, dict] = {
    "locked": {
        "name": "Locked",
        "device_class": None,
        "icon": None,
        "state_key": "locked",
    },
}


# ---------------------------------------------------------------------------
# Service schemas -- device-facing (called by the app)
#
# All fields are snake_case per HA convention. The Zendo app's HA client
# converts camelCase -> snake_case when calling services.
# extra=REMOVE_EXTRA for forward compatibility when new fields are added.
# ---------------------------------------------------------------------------

SERVICE_REGISTER_MANAGED_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_name"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_brand"): vol.Any(None, cv.string),
        vol.Required("device_type"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("device_model_name"): vol.Any(None, cv.string),
        vol.Required("device_os"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("app_version"): vol.All(cv.string, vol.Length(min=1)),
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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _build_model(device_brand: str | None, device_model_name: str | None) -> str | None:
    parts = [p for p in [device_brand, device_model_name] if p]
    return " ".join(parts) if parts else None


class BNGntManagedDeviceRegistry:
    """Tracks registered managed devices, their state, and online/offline status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._devices: dict[str, dict] = {}
        self._pending_commands: dict[str, asyncio.Future] = {}
        self._offline_check_unsub: callable | None = None

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        """Restore persisted devices and start the offline checker."""
        persisted: dict = self._entry.data.get(CONF_MANAGED_DEVICES, {})
        for uid, info in persisted.items():
            self._restore_device(uid, info)

        self._offline_check_unsub = async_track_time_interval(
            self._hass, self._check_offline, timedelta(seconds=60)
        )

    def stop(self) -> None:
        """Stop the offline checker and cancel pending commands."""
        if self._offline_check_unsub:
            self._offline_check_unsub()
            self._offline_check_unsub = None

        for future in self._pending_commands.values():
            if not future.done():
                future.cancel()
        self._pending_commands.clear()

    # -- public API ----------------------------------------------------------

    @property
    def devices(self) -> dict[str, dict]:
        return self._devices

    def get_device(self, uid: str) -> dict | None:
        return self._devices.get(uid)

    def register_device(
        self,
        profile_id: str,
        device_id: str,
        device_name: str,
        device_brand: str | None,
        device_type: str,
        device_model_name: str | None,
        device_os: str,
        app_version: str,
    ) -> str:
        """Register or update a managed device. Returns the device UID.

        Called on kiosk-mode enable AND on every control-backend connect
        (idempotent). On first call: creates HA device + sensor entities.
        On subsequent calls: updates device metadata (name, model, version)
        and marks it back online if it was offline.
        """
        uid = f"{profile_id}_{device_id}"
        is_new = uid not in self._devices

        model = _build_model(device_brand, device_model_name)

        dev_reg = dr.async_get(self._hass)
        ha_device = dev_reg.async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={(DOMAIN, uid)},
            name=device_name,
            manufacturer="Zendo",
            model=model,
            sw_version=app_version,
        )

        if is_new:
            self._devices[uid] = {
                "profile_id": profile_id,
                "device_id": device_id,
                "device_name": device_name,
                "device_brand": device_brand,
                "device_type": device_type,
                "device_model_name": device_model_name,
                "device_os": device_os,
                "app_version": app_version,
                "online": True,
                "last_seen": time.time(),
                "state": {},
                "ha_device_id": ha_device.id,
            }
            self._create_entities(uid, available=True)
        else:
            dev = self._devices[uid]
            dev.update(
                {
                    "device_name": device_name,
                    "device_brand": device_brand,
                    "device_type": device_type,
                    "device_model_name": device_model_name,
                    "device_os": device_os,
                    "app_version": app_version,
                    "online": True,
                    "last_seen": time.time(),
                    "ha_device_id": ha_device.id,
                }
            )
            dev_reg.async_update_device(
                ha_device.id,
                name=device_name,
                model=model,
                sw_version=app_version,
            )
            self._set_entities_available(uid, True)

        self._persist_devices()
        return uid

    def unregister_device(self, profile_id: str, device_id: str) -> None:
        """Mark a device unavailable (intentional disconnect).

        Called when kiosk mode is disabled. The device and its HA entities
        stay in the registry (not deleted) so automations referencing them
        don't break -- they just show as unavailable.
        """
        uid = f"{profile_id}_{device_id}"
        device = self._devices.get(uid)
        if device:
            device["online"] = False
            self._set_entities_available(uid, False)

    def update_state(self, profile_id: str, device_id: str, state: dict) -> None:
        """Update device state from a heartbeat or state-change push."""
        uid = f"{profile_id}_{device_id}"
        device = self._devices.get(uid)
        if not device:
            _LOGGER.warning("State update for unknown managed device %s", uid)
            return

        was_offline = not device["online"]
        device["state"] = state
        device["last_seen"] = time.time()
        device["online"] = True

        if "app_version" in state and state["app_version"] != device.get("app_version"):
            device["app_version"] = state["app_version"]
            dev_reg = dr.async_get(self._hass)
            if device.get("ha_device_id"):
                dev_reg.async_update_device(
                    device["ha_device_id"], sw_version=state["app_version"]
                )

        if was_offline:
            self._set_entities_available(uid, True)

        self._update_entity_values(uid)

    async def send_command(
        self, uid: str, method: str, params: dict | None = None
    ) -> dict:
        """Fire a JSON-RPC command event and wait for the device response.

        The round-trip works via the HA event bus:
        1. Fire ``bngnt_managed_device_command`` with a JSON-stringified
           JSON-RPC request and the target ``profile_id`` + ``device_id``.
        2. The Zendo app receives it via WebSocket, processes the RPC,
           and calls ``zendo.managed_device_command_response`` with the
           JSON-RPC response (same ``id``).
        3. ``handle_command_response`` resolves the Future stored here,
           unblocking this coroutine.

        Raises ``HomeAssistantError`` on timeout or if the device returns
        a JSON-RPC error object.
        """
        device = self._devices.get(uid)
        if not device:
            raise HomeAssistantError(f"Unknown managed device: {uid}")
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
            raise HomeAssistantError(
                f"Device '{device['device_name']}' did not respond "
                f"within {COMMAND_TIMEOUT} seconds"
            ) from None
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
            future.set_exception(
                HomeAssistantError(f"Device returned error: {msg}")
            )
        else:
            future.set_result(response.get("result", {}))

    def resolve_ha_device_to_uid(self, ha_device_id: str) -> str | None:
        """Resolve an HA device-registry ID to a managed-device UID."""
        dev_reg = dr.async_get(self._hass)
        device_entry = dev_reg.async_get(ha_device_id)
        if not device_entry:
            return None
        for domain, identifier in device_entry.identifiers:
            if domain == DOMAIN and identifier in self._devices:
                return identifier
        return None

    # -- internal ------------------------------------------------------------

    @callback
    def _check_offline(self, _now) -> None:
        threshold = time.time() - OFFLINE_TIMEOUT
        for uid, device in list(self._devices.items()):
            if device["online"] and device["last_seen"] < threshold:
                _LOGGER.info("Managed device %s went offline", uid)
                device["online"] = False
                self._set_entities_available(uid, False)

    def _restore_device(self, uid: str, info: dict) -> None:
        model = _build_model(info.get("device_brand"), info.get("device_model_name"))

        dev_reg = dr.async_get(self._hass)
        ha_device = dev_reg.async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={(DOMAIN, uid)},
            name=info["device_name"],
            manufacturer="Zendo",
            model=model,
            sw_version=info.get("app_version"),
        )

        self._devices[uid] = {
            **info,
            "online": False,
            "last_seen": 0,
            "state": {},
            "ha_device_id": ha_device.id,
        }

        self._create_entities(uid, available=False)

    def _create_entities(self, uid: str, available: bool) -> None:
        device = self._devices[uid]

        add_sensors = self._hass.data[DOMAIN].get(
            "async_add_managed_device_sensors"
        )
        add_binary_sensors = self._hass.data[DOMAIN].get(
            "async_add_managed_device_binary_sensors"
        )

        if add_sensors:
            add_sensors(
                [
                    BNGntManagedDeviceSensor(
                        self._entry, uid, device, key, prop, self, available
                    )
                    for key, prop in SENSOR_PROPERTIES.items()
                ],
                update_before_add=True,
            )

        if add_binary_sensors:
            add_binary_sensors(
                [
                    BNGntManagedDeviceBinarySensor(
                        self._entry, uid, device, key, prop, self, available
                    )
                    for key, prop in BINARY_SENSOR_PROPERTIES.items()
                ],
                update_before_add=True,
            )

    def _set_entities_available(self, uid: str, available: bool) -> None:
        entities: list = (
            self._hass.data[DOMAIN]
            .get("managed_device_entities", {})
            .get(uid, [])
        )
        for entity in entities:
            entity.set_device_available(available)

    def _update_entity_values(self, uid: str) -> None:
        entities: list = (
            self._hass.data[DOMAIN]
            .get("managed_device_entities", {})
            .get(uid, [])
        )
        for entity in entities:
            entity.notify_state_updated()

    def _persist_devices(self) -> None:
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return
        target_entry = entries[0]

        persisted: dict[str, dict] = {}
        for uid, device in self._devices.items():
            persisted[uid] = {
                "profile_id": device["profile_id"],
                "device_id": device["device_id"],
                "device_name": device["device_name"],
                "device_brand": device.get("device_brand"),
                "device_type": device.get("device_type", ""),
                "device_model_name": device.get("device_model_name"),
                "device_os": device.get("device_os", ""),
                "app_version": device.get("app_version"),
            }

        self._hass.config_entries.async_update_entry(
            target_entry,
            data={**target_entry.data, CONF_MANAGED_DEVICES: persisted},
        )


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

def _device_info_from_dict(uid: str, device_info: dict) -> DeviceInfo:
    model = _build_model(
        device_info.get("device_brand"),
        device_info.get("device_model_name"),
    )
    return DeviceInfo(
        identifiers={(DOMAIN, uid)},
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
        entry: ConfigEntry,
        uid: str,
        device_info: dict,
        prop_key: str,
        prop_def: dict,
        registry: BNGntManagedDeviceRegistry,
        available: bool,
    ) -> None:
        self._uid = uid
        self._registry = registry
        self._state_key = prop_def["state_key"]
        self._device_available = available

        self._attr_unique_id = f"{entry.entry_id}_md_{uid}_{prop_key}"
        self._attr_name = prop_def["name"]
        if prop_def.get("device_class"):
            self._attr_device_class = prop_def["device_class"]
        if prop_def.get("state_class"):
            self._attr_state_class = prop_def["state_class"]
        if prop_def.get("native_unit"):
            self._attr_native_unit_of_measurement = prop_def["native_unit"]
        if prop_def.get("icon"):
            self._attr_icon = prop_def["icon"]
        if prop_def.get("options"):
            self._attr_options = prop_def["options"]

        self._device_info_snapshot = _device_info_from_dict(uid, device_info)

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info_snapshot

    @property
    def native_value(self):
        device = self._registry.get_device(self._uid)
        if not device:
            return None
        return device.get("state", {}).get(self._state_key)

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
        entities.setdefault(self._uid, []).append(self)

    async def async_will_remove_from_hass(self) -> None:
        entities = self.hass.data[DOMAIN].get("managed_device_entities", {})
        device_entities = entities.get(self._uid, [])
        if self in device_entities:
            device_entities.remove(self)


class BNGntManagedDeviceBinarySensor(BinarySensorEntity):
    """A binary sensor entity for a single property of a managed device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        uid: str,
        device_info: dict,
        prop_key: str,
        prop_def: dict,
        registry: BNGntManagedDeviceRegistry,
        available: bool,
    ) -> None:
        self._uid = uid
        self._prop_key = prop_key
        self._registry = registry
        self._state_key = prop_def["state_key"]
        self._device_available = available

        self._attr_unique_id = f"{entry.entry_id}_md_{uid}_{prop_key}"
        self._attr_name = prop_def["name"]
        if prop_def.get("device_class"):
            self._attr_device_class = prop_def["device_class"]
        if prop_def.get("icon"):
            self._attr_icon = prop_def["icon"]

        self._device_info_snapshot = _device_info_from_dict(uid, device_info)

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info_snapshot

    @property
    def is_on(self) -> bool | None:
        device = self._registry.get_device(self._uid)
        if not device:
            return None
        return device.get("state", {}).get(self._state_key)

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
        entities.setdefault(self._uid, []).append(self)

    async def async_will_remove_from_hass(self) -> None:
        entities = self.hass.data[DOMAIN].get("managed_device_entities", {})
        device_entities = entities.get(self._uid, [])
        if self in device_entities:
            device_entities.remove(self)


# ---------------------------------------------------------------------------
# Service handlers -- device-facing
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
    registry.register_device(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
        device_name=call.data["device_name"],
        device_brand=call.data["device_brand"],
        device_type=call.data["device_type"],
        device_model_name=call.data["device_model_name"],
        device_os=call.data["device_os"],
        app_version=call.data["app_version"],
    )
    return {"success": True}


async def handle_unregister_managed_device(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    registry = _get_registry(hass)
    registry.unregister_device(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
    )
    return {"success": True}


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

    registry.update_state(
        profile_id=call.data["profile_id"],
        device_id=call.data["device_id"],
        state=state,
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# Service handlers -- automation-facing
#
# Each handler resolves the HA device-registry ID (from the ``device``
# field / device selector) to a managed-device UID, builds JSON-RPC
# params from the service call data, and delegates to ``send_command``.
#
# Field name mapping: HA uses snake_case (``color_scheme``), the JSON-RPC
# protocol uses camelCase (``colorScheme``). Conversion happens here.
#
# Settings-style RPC methods (screensaver.set, screen.set, audio.set)
# accept partial params -- only the fields present in the service call
# are included. The device leaves omitted settings unchanged.
# ---------------------------------------------------------------------------

def _resolve_devices_for_command(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[BNGntManagedDeviceRegistry, list[str]]:
    """Resolve the ``device`` field to managed-device UIDs.

    The ``device`` field in services.yaml uses ``selector: device:`` with
    ``multiple: true``, so the value is a list of HA device IDs.
    """
    registry = _get_registry(hass)
    ha_device_ids = call.data.get("device", [])
    if isinstance(ha_device_ids, str):
        ha_device_ids = [ha_device_ids]

    uids: list[str] = []
    for ha_device_id in ha_device_ids:
        uid = registry.resolve_ha_device_to_uid(ha_device_id)
        if uid:
            uids.append(uid)

    if not uids:
        raise ServiceValidationError(
            "No managed devices found in the selected targets. "
            "Please select one or more Zendo tablets."
        )
    return registry, uids


async def _fan_out_command(
    registry: BNGntManagedDeviceRegistry,
    uids: list[str],
    method: str,
    params: dict | None = None,
) -> None:
    """Send a command to multiple devices in parallel."""
    await asyncio.gather(*(
        registry.send_command(uid, method, params)
        for uid in uids
    ))


async def handle_managed_device_screensaver_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "mode" in call.data:
        params["mode"] = call.data["mode"]
    if "timer" in call.data:
        params["timer"] = call.data["timer"]

    await _fan_out_command(registry, uids, "screensaver.set", params)


async def handle_managed_device_screen_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "brightness" in call.data:
        params["brightness"] = call.data["brightness"]
    if "color_scheme" in call.data:
        params["colorScheme"] = call.data["color_scheme"]

    await _fan_out_command(registry, uids, "screen.set", params)


async def handle_managed_device_screen_wake_up(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, uids, "screen.wakeUp")


async def handle_managed_device_audio_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)

    params: dict = {}
    if "volume" in call.data:
        params["volume"] = call.data["volume"]

    await _fan_out_command(registry, uids, "audio.set", params)


async def handle_managed_device_app_reload(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, uids, "app.reload")


async def handle_managed_device_app_lock(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)

    params: dict = {"pin": call.data["pin"]}
    if "message" in call.data:
        params["message"] = call.data["message"]

    await _fan_out_command(registry, uids, "app.lock", params)


async def handle_managed_device_app_unlock(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    registry, uids = _resolve_devices_for_command(hass, call)
    await _fan_out_command(registry, uids, "app.unlock")
