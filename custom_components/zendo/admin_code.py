"""Admin codes of managed devices: the hub's copy, the report command and the
admin-only services.

The device reports its code, or that it has none, whenever it changes and at
every connection. The hub keeps the last report, so the code can be looked up
while the device is offline. See docs/managed-devices.md, "Admin Code".

A code must never reach a log line, an error message, an entity, an attribute,
a state, an event or ``_serialize_managed_device``. Only its status does: the
list item and ``bngnt_managed_device_updated`` carry ``admin_code_status``.
Validation that fails on a code is done in the handlers with fixed messages,
because a voluptuous schema error, humanized, ends with the value it rejected
and Home Assistant logs it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from typing import Any, Literal, TypedDict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .managed_devices import (
    COMMAND_TIMEOUT,
    JSONRPC_METHOD_NOT_FOUND,
    BNGntManagedDeviceCommandError,
    BNGntManagedDeviceRegistry,
    BNGntManagedDeviceTimeoutError,
    _get_registry,
    _resolve_devices_for_command,
)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.managed_device_admin_codes"

WS_TYPE_REPORT_ADMIN_CODE = f"{DOMAIN}/report_managed_device_admin_code"

ADMIN_CODE_METHOD = "adminCode.set"

# JSON-RPC errors a device answers adminCode.set with, besides Method not
# found. Not supported: it's in more than one property, or it can't make the
# change for another reason it can only resolve itself.
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_NOT_SUPPORTED = -32001

# The app and the Homey app know the same three, so keep all three in sync.
STATUS_SET = "set"
STATUS_NOT_SET = "not_set"
STATUS_UNKNOWN = "unknown"
ADMIN_CODE_STATUSES = (STATUS_SET, STATUS_NOT_SET, STATUS_UNKNOWN)

AdminCodeStatus = Literal["set", "not_set", "unknown"]

_ADMIN_CODE_PATTERN = re.compile(r"[0-9]{4}|[0-9]{6}|[0-9]{8}")

INVALID_ADMIN_CODE_MESSAGE = "The admin code must be exactly 4, 6 or 8 digits."


def is_valid_admin_code(value: Any) -> bool:
    """Return whether ``value`` is 4, 6 or 8 ASCII digits."""
    return isinstance(value, str) and _ADMIN_CODE_PATTERN.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class BNGntAdminCodeEntry(TypedDict):
    """The last admin code a managed device reported."""

    # None: the device reported that it has no admin code.
    code: str | None
    # ISO 8601, UTC: when the device last reported it.
    reported_at: str


def admin_code_status(entry: BNGntAdminCodeEntry | None) -> AdminCodeStatus:
    """Return the status an entry reads as: no entry is unknown."""
    if entry is None:
        return STATUS_UNKNOWN
    return STATUS_SET if entry["code"] is not None else STATUS_NOT_SET


def _restored_entry(value: Any) -> BNGntAdminCodeEntry | None:
    if not isinstance(value, dict):
        return None
    code = value.get("code")
    reported_at = value.get("reported_at")
    if not (code is None or is_valid_admin_code(code)):
        return None
    if not isinstance(reported_at, str):
        return None
    return {"code": code, "reported_at": reported_at}


class BNGntAdminCodeStore:
    """The hub's copy of each managed device's admin code.

    Keyed by managed device ID. Kept in its own store, a file only its owner
    can read, rather than in the config entry's data, which anything that
    reads the entry would see.

    No entry for a record: its device hasn't reported (status unknown). An
    entry whose code is None: its device has no admin code (status not_set).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, BNGntAdminCodeEntry]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._entries: dict[str, BNGntAdminCodeEntry] = {}
        self._dirty = False

    async def async_load(self, managed_device_ids: Iterable[str]) -> None:
        """Load the entries, dropping any whose record is gone."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            data = {}

        known = set(managed_device_ids)
        self._entries = {}
        for key, value in data.items():
            entry = _restored_entry(value)
            if key in known and entry is not None:
                self._entries[key] = entry

        if len(self._entries) != len(data):
            self._schedule_save()

    async def async_flush(self) -> None:
        """Write a pending change now.

        Called on unload, so that a delayed write can't land after
        ``async_remove_store`` has removed the file.
        """
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    def get(self, key: str) -> BNGntAdminCodeEntry | None:
        return self._entries.get(key)

    def status(self, key: str) -> AdminCodeStatus:
        return admin_code_status(self._entries.get(key))

    @callback
    def report(self, key: str, code: str | None) -> None:
        """Keep what a device reported: its code, or None for no code."""
        self._entries[key] = {
            "code": code,
            "reported_at": dt_util.utcnow().isoformat(),
        }
        self._schedule_save()

    @callback
    def remove(self, key: str) -> None:
        if self._entries.pop(key, None) is not None:
            self._schedule_save()

    @callback
    def keep_on_replace(self, replacement_key: str, target_key: str) -> None:
        """Settle the target's entry when the replacement takes it over.

        The old device's code state wins, a code, none or unknown: the target
        keeps its own entry, or stays without one, and the settings restore
        hands that state to the replacement. The replacement's entry is never
        moved to the target: it goes with the record it leaves behind.
        """
        if self._entries.pop(replacement_key, None) is not None:
            self._schedule_save()

    @callback
    def _schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data_to_save)

    @callback
    def _data_to_save(self) -> dict[str, BNGntAdminCodeEntry]:
        self._dirty = False
        return dict(self._entries)


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()


# ---------------------------------------------------------------------------
# WebSocket command -- device-facing (called by the app)
#
# A command rather than a service: every service call fires a ``call_service``
# event with its data, which the recorder keeps, so each code a device reported
# would be recorded. Not admin-only, like update_managed_device_state, since the
# app may sign in as a user who isn't an administrator.
# ---------------------------------------------------------------------------

@websocket_api.websocket_command(
    vol.All(
        vol.Schema(
            {
                vol.Required("type"): WS_TYPE_REPORT_ADMIN_CODE,
                vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
                vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
                # Any value, checked in the handler: a schema error would put
                # the value in the error and the log.
                vol.Required("admin_code"): object,
            },
            extra=vol.REMOVE_EXTRA,
        )
    )
)
@callback
def ws_report_managed_device_admin_code(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Keep the admin code a device reports, or that it has none.

    Responds ``registered: false`` when no record has the device attached, as
    ``update_managed_device_state`` does, and keeps nothing. A report that
    changes the record's admin code status fires ``bngnt_managed_device_updated``.
    """
    registry: BNGntManagedDeviceRegistry | None = hass.data.get(DOMAIN, {}).get(
        "managed_device_registry"
    )
    if registry is None:
        connection.send_error(
            msg["id"],
            websocket_api.ERR_HOME_ASSISTANT_ERROR,
            "Managed devices are not initialized",
        )
        return

    code = msg["admin_code"]
    if code is not None and not is_valid_admin_code(code):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_INVALID_FORMAT,
            "admin_code must be exactly 4, 6 or 8 digits, or null",
        )
        return

    key = registry.find_key(msg["profile_id"], msg["device_id"])
    if key is not None:
        registry.report_admin_code(key, code)

    connection.send_result(msg["id"], {"success": True, "registered": key is not None})


# ---------------------------------------------------------------------------
# Services -- automation-facing, admin only
# ---------------------------------------------------------------------------

def _one_device(value: Any) -> str:
    if isinstance(value, list):
        if len(value) != 1:
            raise vol.Invalid("Select one device")
        value = value[0]
    return cv.string(value)


SERVICE_MANAGED_DEVICE_ADMIN_CODE_GET_SCHEMA = vol.Schema(
    {vol.Required("device"): _one_device},
)

_CONFIGURE_FIELDS = ("device", "code", "remove")

# Every field is checked in the handler. If this schema rejected a call, Home
# Assistant would log the call's data, code included, at debug level, and the
# error could end with the value it rejected.
SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE_SCHEMA = vol.Schema(
    {vol.Optional(field): object for field in _CONFIGURE_FIELDS},
    extra=vol.ALLOW_EXTRA,
)


async def handle_managed_device_admin_code_get(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Return the last admin code the device reported. Works offline."""
    registry = _get_registry(hass)
    key = registry.resolve_ha_device_to_key(call.data["device"])
    if key is None:
        raise ServiceValidationError(
            "The selected device isn't a managed Zendo device. "
            "Please select one Zendo device."
        )

    entry = registry.admin_codes.get(key)
    if entry is None:
        return {"status": STATUS_UNKNOWN, "admin_code": None, "reported_at": None}

    return {
        "status": admin_code_status(entry),
        "admin_code": entry["code"],
        "reported_at": entry["reported_at"],
    }


def _code_from_call(call: ServiceCall) -> str | None:
    """Return the code to set, or None to remove it.

    Raises ``ServiceValidationError`` with a message that never has the code.
    """
    unknown = [field for field in call.data if field not in _CONFIGURE_FIELDS]
    if unknown:
        raise ServiceValidationError(
            f"Unknown fields: {', '.join(sorted(map(str, unknown)))}. "
            "Give device, and either code or remove."
        )

    device = call.data.get("device")
    if not (
        (isinstance(device, str) and device)
        or (
            isinstance(device, list)
            and device
            and all(isinstance(item, str) for item in device)
        )
    ):
        raise ServiceValidationError("Please select one or more Zendo devices.")

    raw_remove = call.data.get("remove")
    try:
        remove = False if raw_remove is None else cv.boolean(raw_remove)
    except vol.Invalid:
        raise ServiceValidationError("remove must be true or false.") from None

    code = call.data.get("code")
    has_code = code is not None and code != ""

    if has_code and remove:
        raise ServiceValidationError(
            "Give a code or turn on remove, not both."
        )
    if not has_code and not remove:
        raise ServiceValidationError(
            "Give a code, or turn on remove to remove the admin code."
        )
    if not has_code:
        return None

    # YAML reads an unquoted 012345 as an octal number, so a number is never
    # taken as a code.
    if not isinstance(code, str):
        raise ServiceValidationError(
            "Write the admin code as text: in YAML, put it in quotes, "
            "so that a code that starts with 0 keeps its zeros."
        )
    if not is_valid_admin_code(code):
        raise ServiceValidationError(INVALID_ADMIN_CODE_MESSAGE)
    return code


def _device_label(
    hass: HomeAssistant, registry: BNGntManagedDeviceRegistry, key: str
) -> str:
    ha_device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, key)})
    if ha_device is not None and ha_device.name_by_user:
        return ha_device.name_by_user
    device = registry.get_device(key)
    return device["device_name"] if device else key


def _command_error_reason(code: Any, removing: bool) -> str:
    if code == JSONRPC_NOT_SUPPORTED:
        if removing:
            return "it declined. Check Admin Code in the device's Settings"
        return (
            "it declined, for example because it's in more than one property: "
            "an admin code works only on a device that's in one property"
        )
    if code == JSONRPC_INVALID_PARAMS:
        return "it didn't accept the code"
    return f"it answered with error {code}"


async def _send_admin_code(
    registry: BNGntManagedDeviceRegistry, key: str, code: str | None
) -> str | None:
    """Send the code (None removes it). Returns why it failed, or None."""
    device = registry.get_device(key)
    if device is None:
        return "it's no longer a managed device"
    if not device["online"]:
        return "it's offline"

    try:
        await registry.send_command(key, ADMIN_CODE_METHOD, {"code": code})
    except BNGntManagedDeviceTimeoutError:
        return (
            f"it didn't answer within {COMMAND_TIMEOUT} seconds. Look up its "
            "admin code to see whether it changed"
        )
    except BNGntManagedDeviceCommandError as err:
        return _command_error_reason(err.code, removing=code is None)
    except HomeAssistantError as err:
        cause = err.__cause__
        if (
            isinstance(cause, BNGntManagedDeviceCommandError)
            and cause.code == JSONRPC_METHOD_NOT_FOUND
        ):
            return "it needs a newer version of the Zendo app"
        # send_command's own messages, which never carry the params.
        return str(err)
    return None


def _failure_message(
    removing: bool, failures: list[tuple[str, str]], total: int
) -> str:
    action = "remove the admin code from" if removing else "set the admin code on"

    if total == 1:
        label, reason = failures[0]
        return f"Couldn't {action} '{label}': {reason}."

    reasons = " ".join(f"'{label}': {reason}." for label, reason in failures)
    message = f"Couldn't {action} {len(failures)} of {total} devices. {reasons}"

    succeeded = total - len(failures)
    if succeeded == 0:
        return message
    if removing:
        if succeeded == 1:
            return f"{message} It was removed from the other device."
        return f"{message} It was removed from the other {succeeded} devices."
    if succeeded == 1:
        return f"{message} The other device took it."
    return f"{message} The other {succeeded} devices took it."


async def handle_managed_device_admin_code_configure(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Set or remove the admin code on each device.

    A device that answers success keeps it, whatever the others do, and has
    already reported it: the app reports before it answers. The call then
    fails naming the devices that didn't take it, and why.
    """
    code = _code_from_call(call)
    registry, keys = _resolve_devices_for_command(hass, call)
    keys = list(dict.fromkeys(keys))

    reasons = await asyncio.gather(
        *(_send_admin_code(registry, key, code) for key in keys)
    )

    failures = [
        (_device_label(hass, registry, key), reason)
        for key, reason in zip(keys, reasons, strict=True)
        if reason is not None
    ]
    if failures:
        raise HomeAssistantError(_failure_message(code is None, failures, len(keys)))
