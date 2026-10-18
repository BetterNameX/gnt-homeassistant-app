"""Settings backups of managed devices: the hub's copy, the restore a replace
leaves on the record taken over, and the commands devices use for both.

A device reports its settings backup, a JSON object owned by the app that the
integration never reads. A replace leaves a restore on the record taken over:
a snapshot of that record's latest backup and admin code status and code,
which the new device fetches, applies and completes. See docs/managed-devices.md, "Settings Backup".

None of it may reach a log line, an error message, an entity, an attribute, a
state, an event or ``_serialize_managed_device``: a backup is the app's, and a
restore carries an admin code. Validation that fails on a backup is done in
the handler with a fixed message, as in ``admin_code.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any, NotRequired, TypedDict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .admin_code import (
    ADMIN_CODE_STATUSES,
    STATUS_SET,
    STATUS_UNKNOWN,
    AdminCodeStatus,
    is_valid_admin_code,
)
from .const import DOMAIN
from .managed_devices import BNGntManagedDeviceRegistry

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.managed_device_settings_backups"

WS_TYPE_REPORT_SETTINGS_BACKUP = f"{DOMAIN}/report_managed_device_settings_backup"
WS_TYPE_GET_SETTINGS_RESTORE = f"{DOMAIN}/get_managed_device_settings_restore"
WS_TYPE_COMPLETE_SETTINGS_RESTORE = (
    f"{DOMAIN}/complete_managed_device_settings_restore"
)

# The largest backup taken, in bytes of its compact JSON in UTF-8. The Homey
# app and the Zendo app use the same cap, measured the same way, so keep all
# three in sync.
SETTINGS_BACKUP_MAX_BYTES = 16384

INVALID_SETTINGS_BACKUP_MESSAGE = (
    "settings_backup must be a JSON object of at most "
    f"{SETTINGS_BACKUP_MAX_BYTES} bytes"
)


def is_valid_settings_backup(value: Any) -> bool:
    """Return whether ``value`` is a JSON object within the size cap."""
    if not isinstance(value, dict):
        return False
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return False
    return size <= SETTINGS_BACKUP_MAX_BYTES


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class BNGntSettingsRestore(TypedDict):
    """What a replace leaves for the device that took the record over.

    Its data is the record's at the replace that made it, and a later replace
    before it's completed passes the same data on.
    """

    # uuid4 hex, new at each replace. The device completes the restore with
    # it, so a completion meant for an older restore leaves a newer one in
    # place.
    restore_id: str
    # The record's device name before the replace.
    previous_device_name: str
    # The record's latest backup when it was replaced, or None for none.
    settings_backup: dict[str, Any] | None
    # The record's admin code status when it was replaced. Only set leaves the
    # new device with a code: for not_set and unknown it removes its own.
    admin_code_status: AdminCodeStatus
    # The code when admin_code_status is set, and None otherwise.
    admin_code: str | None
    # ISO 8601, UTC: when the record was last replaced.
    created_at: str


class BNGntSettingsBackupEntry(TypedDict):
    """A record's latest settings backup, and its pending restore."""

    # Both None when the record's device hasn't reported a backup, and the
    # entry is there only for its restore.
    settings_backup: dict[str, Any] | None
    # ISO 8601, UTC: when the device last reported its backup.
    reported_at: str | None
    restore: NotRequired[BNGntSettingsRestore]


def _restored_restore(value: Any) -> BNGntSettingsRestore | None:
    if not isinstance(value, dict):
        return None
    restore_id = value.get("restore_id")
    previous_device_name = value.get("previous_device_name")
    settings_backup = value.get("settings_backup")
    admin_code = value.get("admin_code")
    created_at = value.get("created_at")
    if not (isinstance(restore_id, str) and restore_id):
        return None
    if not isinstance(previous_device_name, str):
        return None
    if not (settings_backup is None or isinstance(settings_backup, dict)):
        return None
    if not (admin_code is None or is_valid_admin_code(admin_code)):
        return None
    if not isinstance(created_at, str):
        return None

    # A restore without a status never hands over not_set: with a code it's
    # set, and without one unknown.
    admin_code_status = value.get("admin_code_status")
    if "admin_code_status" not in value:
        admin_code_status = STATUS_UNKNOWN if admin_code is None else STATUS_SET
    if admin_code_status not in ADMIN_CODE_STATUSES:
        return None
    if (admin_code_status == STATUS_SET) != (admin_code is not None):
        return None

    return {
        "restore_id": restore_id,
        "previous_device_name": previous_device_name,
        "settings_backup": settings_backup,
        "admin_code_status": admin_code_status,
        "admin_code": admin_code,
        "created_at": created_at,
    }


def _restored_entry(value: Any) -> BNGntSettingsBackupEntry | None:
    """Return the valid parts of a stored entry, or None if none are."""
    if not isinstance(value, dict):
        return None

    settings_backup = value.get("settings_backup")
    reported_at = value.get("reported_at")
    if not (isinstance(settings_backup, dict) and isinstance(reported_at, str)):
        settings_backup = None
        reported_at = None

    restore = _restored_restore(value.get("restore"))
    if settings_backup is None and restore is None:
        return None

    entry: BNGntSettingsBackupEntry = {
        "settings_backup": settings_backup,
        "reported_at": reported_at,
    }
    if restore is not None:
        entry["restore"] = restore
    return entry


class BNGntSettingsBackupStore:
    """The hub's copy of each managed device's settings backup.

    Keyed by managed device ID, in its own store, a file only its owner can
    read, like the admin codes. An entry always has a backup, a restore or
    both. No entry for a record: its device hasn't reported a backup, and it
    has no restore.

    Entries are replaced, never changed in place: ``_data_to_save`` hands the
    store a shallow copy.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, BNGntSettingsBackupEntry]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._entries: dict[str, BNGntSettingsBackupEntry] = {}
        self._dirty = False

    async def async_load(self, managed_device_ids: Iterable[str]) -> None:
        """Load the entries, dropping any whose record is gone."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            data = {}

        known = set(managed_device_ids)
        self._entries = {}
        changed = False
        for key, value in data.items():
            entry = _restored_entry(value)
            if key in known and entry is not None:
                self._entries[key] = entry
            if key not in known or entry != value:
                changed = True

        if changed:
            self._schedule_save()

    async def async_flush(self) -> None:
        """Write a pending change now.

        Called on unload, so that a delayed write can't land after
        ``async_remove_store`` has removed the file.
        """
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    def has_restore(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and "restore" in entry

    def restore_for(self, key: str) -> BNGntSettingsRestore | None:
        entry = self._entries.get(key)
        return entry.get("restore") if entry is not None else None

    @callback
    def report(self, key: str, settings_backup: dict[str, Any]) -> None:
        """Keep the backup a device reported. A pending restore stays."""
        entry: BNGntSettingsBackupEntry = {
            "settings_backup": settings_backup,
            "reported_at": dt_util.utcnow().isoformat(),
        }
        restore = self.restore_for(key)
        if restore is not None:
            entry["restore"] = restore
        self._entries[key] = entry
        self._schedule_save()

    @callback
    def remove(self, key: str) -> None:
        if self._entries.pop(key, None) is not None:
            self._schedule_save()

    @callback
    def on_replace(
        self,
        replacement_key: str,
        target_key: str,
        previous_device_name: str,
        admin_code_status: AdminCodeStatus,
        admin_code: str | None,
    ) -> None:
        """Leave a restore on the target for the device that takes it over.

        ``previous_device_name``, ``admin_code_status`` and ``admin_code`` are
        the target's, read before the replace changes anything. The restore is
        a snapshot of them and of the target's latest backup. Every replace
        leaves one, with no backup and the status unknown too: the new device
        still has to remove its own code.

        A restore still pending on the target is passed on instead, under a
        new ID: the original device's data stays until a device completes it,
        so a transfer that failed or was abandoned never loses it. The older
        ID is then refused.

        The target keeps its latest backup until the new device reports its
        own. The replacement's entry goes with the record it leaves behind.
        """
        self._entries.pop(replacement_key, None)

        entry = self._entries.get(target_key)
        settings_backup = entry["settings_backup"] if entry is not None else None
        reported_at = entry["reported_at"] if entry is not None else None
        pending = entry.get("restore") if entry is not None else None

        restore: BNGntSettingsRestore
        if pending is not None:
            restore = {
                "restore_id": uuid.uuid4().hex,
                "previous_device_name": pending["previous_device_name"],
                "settings_backup": pending["settings_backup"],
                "admin_code_status": pending["admin_code_status"],
                "admin_code": pending["admin_code"],
                "created_at": dt_util.utcnow().isoformat(),
            }
        else:
            restore = {
                "restore_id": uuid.uuid4().hex,
                "previous_device_name": previous_device_name,
                "settings_backup": settings_backup,
                "admin_code_status": admin_code_status,
                "admin_code": admin_code if admin_code_status == STATUS_SET else None,
                "created_at": dt_util.utcnow().isoformat(),
            }

        self._entries[target_key] = {
            "settings_backup": settings_backup,
            "reported_at": reported_at,
            "restore": restore,
        }
        self._schedule_save()

    @callback
    def complete(self, key: str, restore_id: str) -> bool:
        """Drop the record's restore if it is ``restore_id``.

        Returns whether it did. Any other ID, and a restore already
        completed, change nothing.
        """
        entry = self._entries.get(key)
        restore = entry.get("restore") if entry is not None else None
        if restore is None or restore["restore_id"] != restore_id:
            return False

        if entry["settings_backup"] is None:
            del self._entries[key]
        else:
            self._entries[key] = {
                "settings_backup": entry["settings_backup"],
                "reported_at": entry["reported_at"],
            }
        self._schedule_save()
        return True

    @callback
    def _schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data_to_save)

    @callback
    def _data_to_save(self) -> dict[str, BNGntSettingsBackupEntry]:
        self._dirty = False
        return dict(self._entries)


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()


# ---------------------------------------------------------------------------
# WebSocket commands -- device-facing (called by the app)
#
# Commands rather than services: every service call fires a ``call_service``
# event with its data, which the recorder keeps, so a service would record
# each backup a device reports. The restore's answer carries an admin code,
# and an automation or script that called a service would keep its response
# in its trace. Not admin-only, like update_managed_device_state, since the
# app may sign in as a user who isn't an administrator.
#
# Never log a message or an answer here.
# ---------------------------------------------------------------------------

_DEVICE_IDS = {
    vol.Required("profile_id"): vol.All(cv.string, vol.Length(min=1)),
    vol.Required("device_id"): vol.All(cv.string, vol.Length(min=1)),
}


def _registry_or_error(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> BNGntManagedDeviceRegistry | None:
    registry: BNGntManagedDeviceRegistry | None = hass.data.get(DOMAIN, {}).get(
        "managed_device_registry"
    )
    if registry is None:
        connection.send_error(
            msg["id"],
            websocket_api.ERR_HOME_ASSISTANT_ERROR,
            "Managed devices are not initialized",
        )
    return registry


@websocket_api.websocket_command(
    vol.All(
        vol.Schema(
            {
                vol.Required("type"): WS_TYPE_REPORT_SETTINGS_BACKUP,
                **_DEVICE_IDS,
                # Any value, checked in the handler: a schema error would put
                # the value in the error and the log.
                vol.Required("settings_backup"): object,
            },
            extra=vol.REMOVE_EXTRA,
        )
    )
)
@callback
def ws_report_managed_device_settings_backup(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Keep the settings backup a device reports.

    Responds ``registered: false`` when no record has the device attached, as
    ``update_managed_device_state`` does, and keeps nothing. A report changes
    nothing else on the record, and fires no event.
    """
    registry = _registry_or_error(hass, connection, msg)
    if registry is None:
        return

    settings_backup = msg["settings_backup"]
    if not is_valid_settings_backup(settings_backup):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_INVALID_FORMAT,
            INVALID_SETTINGS_BACKUP_MESSAGE,
        )
        return

    key = registry.find_key(msg["profile_id"], msg["device_id"])
    if key is not None:
        registry.settings_backups.report(key, settings_backup)

    connection.send_result(msg["id"], {"success": True, "registered": key is not None})


@websocket_api.websocket_command(
    vol.All(
        vol.Schema(
            {vol.Required("type"): WS_TYPE_GET_SETTINGS_RESTORE, **_DEVICE_IDS},
            extra=vol.REMOVE_EXTRA,
        )
    )
)
@callback
def ws_get_managed_device_settings_restore(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the restore pending on the device's record, or null for none."""
    registry = _registry_or_error(hass, connection, msg)
    if registry is None:
        return

    key = registry.find_key(msg["profile_id"], msg["device_id"])
    restore = registry.settings_backups.restore_for(key) if key is not None else None

    connection.send_result(
        msg["id"],
        {
            "success": True,
            "registered": key is not None,
            "restore": (
                None
                if restore is None
                else {
                    "restore_id": restore["restore_id"],
                    "previous_device_name": restore["previous_device_name"],
                    "settings_backup": restore["settings_backup"],
                    "admin_code_status": restore["admin_code_status"],
                    "admin_code": restore["admin_code"],
                }
            ),
        },
    )


@websocket_api.websocket_command(
    vol.All(
        vol.Schema(
            {
                vol.Required("type"): WS_TYPE_COMPLETE_SETTINGS_RESTORE,
                **_DEVICE_IDS,
                vol.Required("restore_id"): vol.All(cv.string, vol.Length(min=1)),
            },
            extra=vol.REMOVE_EXTRA,
        )
    )
)
@callback
def ws_complete_managed_device_settings_restore(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Drop the record's restore once the device has applied it.

    ``completed: false`` when the record's restore isn't ``restore_id``: one
    already completed, or an older one a second replace has replaced.
    """
    registry = _registry_or_error(hass, connection, msg)
    if registry is None:
        return

    key = registry.find_key(msg["profile_id"], msg["device_id"])
    completed = key is not None and registry.settings_backups.complete(
        key, msg["restore_id"]
    )

    connection.send_result(
        msg["id"],
        {"success": True, "registered": key is not None, "completed": completed},
    )
