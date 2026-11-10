"""Device Lock PINs of managed devices: the hub's copy, which an admin looks up.

A device locked behind a PIN (``app.lock``) reports its PIN while it's locked,
or that it isn't locked, at every change and every connection, and before it
answers a lock or an unlock. The hub keeps the last report, so the PIN can be
looked up while the device is offline. The report and the lookup are the
``zendo/v2/`` commands in ``device_api.py``, and there's no v1 form. The hub
never keeps the PIN it sends with ``app.lock``: the device reports it. See
docs/managed-devices.md, "Device Lock PIN".

A PIN must never reach a log line, an error message, an entity, an attribute,
a state, an event, ``_serialize_managed_device`` or the list. Unlike the admin
code, not even its status does: the record's state already says whether the
device is locked. Validation that fails on a PIN is done in the handlers with a
fixed message.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback

from .admin_code import BNGntReportedCodeStore, async_remove_reported_code_store
from .const import DOMAIN
from .device_auth import UNKNOWN_APP_LOCK_PIN_LOOKUP
from .managed_devices import BNGntManagedDeviceRegistry

STORAGE_KEY = f"{DOMAIN}.managed_device_app_lock_pins"
# The PIN's name in each entry of the store.
APP_LOCK_PIN_FIELD = "pin"

INVALID_REPORTED_APP_LOCK_PIN_MESSAGE = "pin must be exactly 4, 6 or 8 digits, or null"


class BNGntAppLockPinStore(BNGntReportedCodeStore):
    """The hub's copy of each managed device's Device Lock PIN.

    Each entry is ``{"pin": <the PIN, or None>, "reported_at": ...}``. No
    entry for a record: its device hasn't reported (status unknown). An entry
    whose PIN is None: its device reported that it isn't locked (status
    not_set).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, STORAGE_KEY, APP_LOCK_PIN_FIELD)

    @callback
    def remove_on_replace(self, replacement_key: str, target_key: str) -> None:
        """Remove both records' entries when the replacement takes the target over.

        The new device starts unlocked, and reports as soon as it has taken the
        record over: until then, the target's PIN is unknown. The replacement's
        entry goes with the record it leaves behind.
        """
        self.remove(target_key)
        self.remove(replacement_key)


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await async_remove_reported_code_store(hass, STORAGE_KEY)


def report_app_lock_pin(
    registry: BNGntManagedDeviceRegistry,
    profile_id: str,
    device_id: str,
    pin: str | None,
) -> dict[str, Any]:
    """Keep a checked PIN, or None for not locked, for the device's record.

    Keeps nothing when no record has the device attached. Fires no event.
    """
    key = registry.find_key(profile_id, device_id)
    if key is not None:
        registry.app_lock_pins.report(key, pin)
    return {"success": True, "registered": key is not None}


def app_lock_pin_lookup(
    registry: BNGntManagedDeviceRegistry, key: str
) -> dict[str, Any] | None:
    """Return the last PIN the record's device reported, or None for no record.

    ``{"status", "pin", "reported_at"}``, read only from the store, so it works
    while the device is offline. With no report, the status is unknown, and the
    PIN and ``reported_at`` are None.
    """
    if registry.get_device(key) is None:
        return None
    entry = registry.app_lock_pins.get(key)
    if entry is None:
        return dict(UNKNOWN_APP_LOCK_PIN_LOOKUP)
    return {
        "status": registry.app_lock_pins.status(key),
        "pin": entry[APP_LOCK_PIN_FIELD],
        "reported_at": entry["reported_at"],
    }
