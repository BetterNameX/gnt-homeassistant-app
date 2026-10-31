"""Admin codes of managed devices: the hub's copy, the report command and the
admin-only action.

The device reports its code, or that it has none, whenever it changes and at
every connection. The hub keeps the last report, so the code can be looked up
while the device is offline. ``admin_code_lookup`` and
``async_send_admin_code`` are shared with the ``zendo/v2/`` admin commands in
``device_api.py``. See docs/managed-devices.md, "Admin Code".

A code must never reach a log line, an error message, an entity, an attribute,
a state, an event or ``_serialize_managed_device``. Only its status does: the
list item and ``bngnt_managed_device_updated`` carry ``admin_code_status``.
Validation that fails on a code is done in the handlers with fixed messages.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .device_auth import UNKNOWN_ADMIN_CODE_LOOKUP
from .managed_devices import (
    COMMAND_TIMEOUT,
    ERROR_APP_UPDATE_REQUIRED,
    ERROR_COMMAND_FAILED,
    ERROR_NO_ANSWER,
    ERROR_NOT_SUPPORTED,
    ERROR_TARGET_NOT_FOUND,
    ERROR_TARGET_OFFLINE,
    BNGntManagedDeviceAppUpdateRequiredError,
    BNGntManagedDeviceCommandError,
    BNGntManagedDeviceNotLinkedError,
    BNGntManagedDeviceOfflineError,
    BNGntManagedDeviceRegistry,
    BNGntManagedDeviceTimeoutError,
    _resolve_devices_for_command,
    async_get_ha_device,
)
from .v1_rules import V1_REFUSED_RESULT, is_v1_refused

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
INVALID_REPORTED_ADMIN_CODE_MESSAGE = "admin_code must be exactly 4, 6 or 8 digits, or null"


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
    can read, rather than in the config entry's data.

    No entry for a record: its device hasn't reported (status unknown). An
    entry whose code is None: its device has no admin code (status not_set).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, BNGntAdminCodeEntry]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._entries: dict[str, BNGntAdminCodeEntry] = {}
        self._dirty = False
        self._closed = False

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

    async def async_close(self) -> None:
        """Write a pending change now, and take no more.

        Called on unload, so that no write, from a call still under way or at
        Home Assistant's final write, can land after ``async_remove_store`` has
        removed the file, or over the one the next setup loads.
        """
        self._closed = True
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
        if self._closed:
            return
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
# A command, never a service, so a report fires no ``call_service`` event. Not
# admin-only, like update_managed_device_state, since the app may sign in as a
# user who isn't an administrator.
# ---------------------------------------------------------------------------

@websocket_api.websocket_command(
    vol.All(
        vol.Schema(
            {
                vol.Required("type"): WS_TYPE_REPORT_ADMIN_CODE,
                vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
                vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
                # Any value, checked in the handler with a fixed message.
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

    if is_v1_refused(
        hass, WS_TYPE_REPORT_ADMIN_CODE, (msg["profile_id"], msg["device_id"])
    ):
        connection.send_result(msg["id"], dict(V1_REFUSED_RESULT))
        return

    code = msg["admin_code"]
    if code is not None and not is_valid_admin_code(code):
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, INVALID_REPORTED_ADMIN_CODE_MESSAGE
        )
        return

    connection.send_result(
        msg["id"],
        report_admin_code(registry, msg["profile_id"], msg["device_id"], code),
    )


def report_admin_code(
    registry: BNGntManagedDeviceRegistry,
    profile_id: str,
    device_id: str,
    code: str | None,
) -> dict[str, Any]:
    """Keep a checked admin code, or None, for the device's record.

    Keeps nothing when no record has the device attached.
    """
    key = registry.find_key(profile_id, device_id)
    if key is not None:
        registry.report_admin_code(key, code)
    return {"success": True, "registered": key is not None}


def admin_code_lookup(
    registry: BNGntManagedDeviceRegistry, key: str
) -> dict[str, Any] | None:
    """Return the last admin code the record's device reported, or None for no record.

    ``{"status", "admin_code", "reported_at"}``, read only from the store, so
    it works while the device is offline. With no report, the status is
    unknown, and the code and ``reported_at`` are None.
    """
    if registry.get_device(key) is None:
        return None
    entry = registry.admin_codes.get(key)
    if entry is None:
        return dict(UNKNOWN_ADMIN_CODE_LOOKUP)
    return {
        "status": admin_code_status(entry),
        "admin_code": entry["code"],
        "reported_at": entry["reported_at"],
    }


@dataclass(frozen=True)
class AdminCodeFailure:
    """Why a device didn't take ``adminCode.set``."""

    # The code ``zendo/v2/configure_managed_device_admin_code`` answers with.
    error: str
    # What ``managed_device_admin_code_configure``'s error says after the
    # device's name. Never the device's own message.
    reason: str


async def async_send_admin_code(
    registry: BNGntManagedDeviceRegistry, key: str, code: str | None
) -> AdminCodeFailure | None:
    """Send the record's device a checked code (None removes it).

    Waits for its answer, up to ``COMMAND_TIMEOUT`` seconds. Returns why it
    didn't take, or None when the device answered success.

    Raises ``BNGntManagedDeviceNotLinkedError`` on a Home Assistant that isn't
    linked, which is about no one device. The ``zendo/v2/`` admin commands never
    get it, since they answer ``not_linked`` before they get here.
    """
    if registry.get_device(key) is None:
        return AdminCodeFailure(ERROR_TARGET_NOT_FOUND, "it's no longer a managed device")

    try:
        await registry.send_command(key, ADMIN_CODE_METHOD, {"code": code})
    except BNGntManagedDeviceNotLinkedError:
        raise
    except BNGntManagedDeviceOfflineError:
        return AdminCodeFailure(ERROR_TARGET_OFFLINE, "it's offline")
    except BNGntManagedDeviceTimeoutError:
        return AdminCodeFailure(
            ERROR_NO_ANSWER,
            f"it didn't answer within {COMMAND_TIMEOUT} seconds. Check its "
            "admin code in the Zendo app to see whether it changed",
        )
    except BNGntManagedDeviceCommandError as err:
        return AdminCodeFailure(
            ERROR_NOT_SUPPORTED if err.code == JSONRPC_NOT_SUPPORTED else ERROR_COMMAND_FAILED,
            _command_error_reason(err.code, removing=code is None),
        )
    except BNGntManagedDeviceAppUpdateRequiredError:
        # It answered Method not found, or it hasn't registered and was sent
        # nothing.
        return AdminCodeFailure(
            ERROR_APP_UPDATE_REQUIRED, "it needs a newer version of the Zendo app"
        )
    except HomeAssistantError as err:
        # The command couldn't be sent, or the integration unloaded before the
        # answer. send_command's own messages never carry the params.
        return AdminCodeFailure(ERROR_NO_ANSWER, str(err))
    return None


# ---------------------------------------------------------------------------
# Services -- automation-facing, admin only
# ---------------------------------------------------------------------------

_CONFIGURE_FIELDS = ("device", "code", "remove")

# Every field is checked in the handler, with a fixed message.
SERVICE_MANAGED_DEVICE_ADMIN_CODE_CONFIGURE_SCHEMA = vol.Schema(
    {vol.Optional(field): object for field in _CONFIGURE_FIELDS},
    extra=vol.ALLOW_EXTRA,
)


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
    ha_device = async_get_ha_device(hass, registry.config_entry_id, key)
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

    On a Home Assistant that isn't linked, no device can take it, so the error
    says that once instead of naming each device.
    """
    code = _code_from_call(call)
    registry, keys = _resolve_devices_for_command(hass, call)
    keys = list(dict.fromkeys(keys))

    outcomes = await asyncio.gather(
        *(async_send_admin_code(registry, key, code) for key in keys)
    )

    failures = [
        (_device_label(hass, registry, key), failure.reason)
        for key, failure in zip(keys, outcomes, strict=True)
        if failure is not None
    ]
    if failures:
        raise HomeAssistantError(_failure_message(code is None, failures, len(keys)))
