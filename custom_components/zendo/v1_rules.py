"""What the calls of an older Zendo app may still do.

Before 2.0.0, the app called services and WebSocket commands that name the
device in the call (v1). From 2.0.0 a device registers and calls the ``zendo/v2/``
commands instead. Some v1 calls are gone in 2.0.0, and the rest stay for devices
that haven't registered, until a later version removes them:

- a v1 call that names a registered or revoked device is refused;
- once Home Assistant is linked to a Zendo site, v1 push setup is refused;
- once an admin's device has registered, every v1 call an admin makes is
  refused.

A refused call that answers gets ``{"success": false, "error": "v2_required"}``,
and one that doesn't raises. Each v1 call is logged once per device, at info, so
the devices still on an older app can be found. See docs/managed-devices.md,
"Older Apps (v1)".
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .device_auth import ERROR_V2_REQUIRED

_LOGGER = logging.getLogger(__name__)

V1_REFUSED_MESSAGE = "This needs a newer version of the Zendo app."
V1_REFUSED_RESULT: dict[str, Any] = {"success": False, "error": ERROR_V2_REQUIRED}


class _DeviceAuth(Protocol):
    @property
    def is_linked(self) -> bool: ...

    @property
    def admin_registered(self) -> bool: ...

    def is_registered(self, profile_id: str, device_id: str) -> bool: ...

    def is_revoked(self, profile_id: str, device_id: str) -> bool: ...


# The calls already logged. When it holds this many it starts over, as on the
# Homey app, so a call may be logged again.
_MAX_LOGGED = 500
_logged: set[tuple[str, str | None, str | None]] = set()


def _log_v1_call(call: str, device: tuple[str, str] | None) -> None:
    profile_id, device_id = device if device is not None else (None, None)
    key = (call, profile_id, device_id)
    if key in _logged:
        return
    if len(_logged) >= _MAX_LOGGED:
        _logged.clear()
    _logged.add(key)
    if device is None:
        _LOGGER.info("%s called by an older Zendo app", call)
    else:
        _LOGGER.info(
            "%s called by an older Zendo app for profile %s, device %s",
            call,
            profile_id,
            device_id,
        )


def is_v1_refused(
    hass: HomeAssistant,
    call: str,
    device: tuple[str, str] | None = None,
    *,
    admin: bool = False,
    push_setup: bool = False,
) -> bool:
    """Return whether a v1 call is refused, logging it.

    ``device`` is the ``(profile_id, device_id)`` the call names.
    ``admin``: the call is one an admin's device makes. ``push_setup``: the call
    sets the push token.
    """
    auth: _DeviceAuth | None = hass.data.get(DOMAIN, {}).get("device_auth")
    if auth is None:
        return False

    _log_v1_call(call, device)

    if push_setup and auth.is_linked:
        return True
    if admin and auth.admin_registered:
        return True
    return device is not None and (auth.is_registered(*device) or auth.is_revoked(*device))


def raise_if_v1_refused(
    hass: HomeAssistant,
    call: str,
    device: tuple[str, str] | None = None,
    *,
    admin: bool = False,
    push_setup: bool = False,
) -> None:
    """Raise ``HomeAssistantError`` when a v1 call that doesn't answer is refused."""
    if is_v1_refused(hass, call, device, admin=admin, push_setup=push_setup):
        raise HomeAssistantError(V1_REFUSED_MESSAGE)
