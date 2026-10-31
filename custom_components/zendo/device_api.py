"""Device registration and the ``zendo/v2/`` WebSocket commands, from 2.0.0.

- **Linking.** Home Assistant belongs to one Zendo site, and is linked by holding
  a push token: the token and its site are stored together, by ``zendo/v2/set_up``
  or from the push token an older version stored. A linked Home Assistant stays
  on its site: there's no unlink. ``zendo/v2/setup_status`` says how it stands.
- **Registering.** A device registers with its device control backend token,
  and gets an API key. The integration keeps the device's encryption public
  key, from the token. The device's own calls then carry the API key, and the
  integration takes its ids from its registration, never from the call.
- **Admin calls** carry an admin-scoped token, for a device that has registered.
- **Events.** A registered device subscribes with ``zendo/v2/subscribe_events``.
  Its commands come in envelopes through the subscriptions, never as bus
  events, encrypted to its public key when they carry a code or a PIN. The
  events meant for every device are forwarded from the bus. An unload tells
  every open subscription with ``bngnt_subscriptions_ended``.
- **Sealed objects.** The admin code in a settings restore answer is sealed to
  the device's public key, and the one in an admin's admin code lookup to the
  public key in the admin's token. The site config encryption key comes the
  same way: to the registration's public key when a device reads it, and to
  the public key in the admin's token when an admin claims it. A ``sealed``
  object in a command's params is sent to the device as it is.
- **Revoking** removes the device's registration, keeps a tombstone so it can't
  register again, tells it with ``bngnt_device_deleted`` and then removes its
  Home Assistant tokens.

Expected failures answer ``{"success": false, "error": <code>}``. Subscriptions,
which have no answer of their own, fail with that code as the error code
instead. A message or answer here is never logged, and a token, an API key or a
key never goes in an error message. See docs/managed-devices.md, "Device
Registration (v2)".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .admin_code import (
    INVALID_REPORTED_ADMIN_CODE_MESSAGE,
    admin_code_lookup,
    async_send_admin_code,
    is_valid_admin_code,
    report_admin_code,
)
from .api_client import BNGntControlBackendNoAnswerError, fetch_site_id
from .const import (
    CONF_CACHED_DEEP_LINK_DESTINATIONS,
    CONF_LINKED_SITE_ID,
    CONF_PUSH_NOTIFICATION_TOKEN,
    DOMAIN,
    INTEGRATION_VERSION,
    SIGNAL_CONFIG_UPDATED,
)
from .device_auth import (
    ERROR_DEVICE_REVOKED,
    ERROR_INVALID_PUSH_TOKEN,
    ERROR_NOT_ADMIN,
    ERROR_NOT_LINKED,
    ERROR_UNKNOWN_API_KEY,
    ERROR_UNSUPPORTED_SCHEME,
    ERROR_WRONG_SITE,
    LINK_ANOTHER_PROPERTY,
    LINK_NOT_LINKED,
    Registration,
    RegistrationError,
    TokenClaims,
    TokenError,
    admin_code_lookup_for_attached_device,
    build_command_envelope,
    check_token,
    choose_scheme,
    claimed_encryption_key_answer,
    encryption_key_answer,
    is_device_token_label,
    link_retry_interval,
    push_token_serial,
    seal_admin_code_answer,
    seal_settings_restore,
    site_link,
    token_kid,
    token_public_key,
    helper_encryption_key,
)
from .device_control_backend_keys import BNGntSigningKeys
from .encryption_key import BNGntEncryptionKeyStore
from .managed_devices import (
    DEVICE_INFO_FIELD_NAMES,
    ERROR_TARGET_NOT_FOUND,
    EVENT_MANAGED_DEVICE_COMMAND,
    EVENT_MANAGED_DEVICE_REMOVED,
    EVENT_MANAGED_DEVICE_REPLACED,
    EVENT_MANAGED_DEVICE_UPDATED,
    REGISTER_DEVICE_INFO_SCHEMA,
    BNGntManagedDeviceRegistry,
    delete_managed_device,
    list_managed_devices,
    parse_command_response,
    parse_state,
    register_managed_device,
    replace_managed_device,
    unregister_managed_device,
    update_managed_device_state,
)
from .registrations import BNGntRegistrationStore
from .settings_backup import (
    INVALID_SETTINGS_BACKUP_MESSAGE,
    complete_settings_restore,
    is_valid_settings_backup,
    report_settings_backup,
    settings_restore,
)

_LOGGER = logging.getLogger(__name__)

WS_PREFIX = f"{DOMAIN}/v2/"

EVENT_DEVICE_DELETED = "bngnt_device_deleted"
EVENT_SUBSCRIPTIONS_ENDED = "bngnt_subscriptions_ended"
EVENT_SITE_CONFIG_MANIFEST_UPDATED = "bngnt_site_config_manifest_updated"
EVENT_DOORBELL_RING = "bngnt_doorbell_ring"
EVENT_DOORBELL_ACTION = "bngnt_doorbell_action"

# Bus events forwarded to every zendo/v2/subscribe_events subscription. A
# managed device recognises its own ids in bngnt_managed_device_replaced, which
# tells it it now holds another record.
_EVENTS_FOR_EVERY_DEVICE = (
    EVENT_SITE_CONFIG_MANIFEST_UPDATED,
    EVENT_DOORBELL_RING,
    EVENT_DOORBELL_ACTION,
    EVENT_MANAGED_DEVICE_REPLACED,
)
# Bus events forwarded to every zendo/v2/subscribe_managed_device_changes
# subscription.
_MANAGED_DEVICE_CHANGE_EVENTS = (
    EVENT_MANAGED_DEVICE_UPDATED,
    EVENT_MANAGED_DEVICE_REMOVED,
    EVENT_MANAGED_DEVICE_REPLACED,
)

# How long a revoked device's tokens stay, in seconds, once its last message is
# queued: removing a token closes its connections, dropping anything still queued.
TOKEN_REMOVAL_DELAY = 1

# The WebSocket error for a v2 command after an unload. The Zendo app matches the code.
ERROR_NOT_SET_UP = "not_set_up"
NOT_SET_UP_MESSAGE = "Zendo isn't set up"


class _Refusal(Exception):
    """An expected failure, answered as ``{"success": false, "error": code}``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _InvalidRequest(Exception):
    """A request that isn't valid, answered as ``invalid_format`` with a fixed
    message that never has a value."""


class _InvalidField(_InvalidRequest):
    """A field that isn't valid, answered with a fixed message naming it."""

    def __init__(self, field: str) -> None:
        super().__init__(f"Invalid {field}")


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@dataclass
class _Subscription:
    connection: websocket_api.ActiveConnection
    msg_id: int
    # The (profile_id, device_id) the subscription was opened for.
    device: tuple[str, str]


class BNGntSubscriptions:
    """The ``zendo/v2/`` subscriptions open on Home Assistant's connections."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._events: dict[int, _Subscription] = {}
        self._changes: dict[int, _Subscription] = {}
        self._next_handle = 0
        self._bus_unsubs: list[CALLBACK_TYPE] = []

    @callback
    def start(self) -> None:
        for event_type in _EVENTS_FOR_EVERY_DEVICE:
            self._bus_unsubs.append(
                self._hass.bus.async_listen(event_type, self._forward_to_events)
            )
        for event_type in _MANAGED_DEVICE_CHANGE_EVENTS:
            self._bus_unsubs.append(
                self._hass.bus.async_listen(event_type, self._forward_to_changes)
            )

    @callback
    def stop(self) -> None:
        """Tell every open subscription it has ended, and drop them."""
        for unsub in self._bus_unsubs:
            unsub()
        self._bus_unsubs.clear()
        for subscriptions in (self._events, self._changes):
            self._send(subscriptions, EVENT_SUBSCRIPTIONS_ENDED, {})
            for subscription in subscriptions.values():
                subscription.connection.subscriptions.pop(subscription.msg_id, None)
            subscriptions.clear()

    @callback
    def add_events(
        self,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        device: tuple[str, str],
    ) -> None:
        self._add(self._events, connection, msg_id, device)

    @callback
    def add_changes(
        self,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        device: tuple[str, str],
    ) -> None:
        self._add(self._changes, connection, msg_id, device)

    @callback
    def _add(
        self,
        subscriptions: dict[int, _Subscription],
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        device: tuple[str, str],
    ) -> None:
        handle = self._next_handle
        self._next_handle += 1
        subscriptions[handle] = _Subscription(connection, msg_id, device)

        @callback
        def _unsub() -> None:
            subscriptions.pop(handle, None)

        connection.subscriptions[msg_id] = _unsub

    @callback
    def end_for_device(self, device: tuple[str, str]) -> None:
        """End every subscription opened for the device."""
        for subscriptions in (self._events, self._changes):
            for handle, subscription in list(subscriptions.items()):
                if subscription.device == device:
                    subscriptions.pop(handle)
                    subscription.connection.subscriptions.pop(subscription.msg_id, None)

    @callback
    def send_to_events(self, event_type: str, data: dict[str, Any]) -> None:
        """Send an event to every ``zendo/v2/subscribe_events`` subscription."""
        self._send(self._events, event_type, data)

    @callback
    def _send(
        self,
        subscriptions: dict[int, _Subscription],
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        message = {"event_type": event_type, "data": data}
        for subscription in list(subscriptions.values()):
            subscription.connection.send_message(
                websocket_api.event_message(subscription.msg_id, message)
            )

    @callback
    def _forward_to_events(self, event: Event) -> None:
        self._send(self._events, event.event_type, dict(event.data))

    @callback
    def _forward_to_changes(self, event: Event) -> None:
        self._send(self._changes, event.event_type, dict(event.data))


# ---------------------------------------------------------------------------
# Device auth
# ---------------------------------------------------------------------------

class BNGntDeviceAuth:
    """The site link, the registrations, the signing keys, the site config
    encryption key and the subscriptions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        keys: BNGntSigningKeys,
        registrations: BNGntRegistrationStore,
        encryption_key: BNGntEncryptionKeyStore,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self.keys = keys
        self.registrations = registrations
        self.encryption_key = encryption_key
        self.subscriptions = BNGntSubscriptions(hass)

    @callback
    def start(self) -> None:
        self.subscriptions.start()

    @callback
    def stop(self) -> None:
        self.subscriptions.stop()

    # -- the link -------------------------------------------------------------

    @property
    def site_id(self) -> str | None:
        site_id = self._entry.data.get(CONF_LINKED_SITE_ID)
        return site_id if isinstance(site_id, str) and site_id else None

    @property
    def is_linked(self) -> bool:
        return self.site_id is not None

    async def async_link_from_stored_push_token(self) -> None:
        """Link from the stored push token, when there's one and no link yet.

        Legacy: this brings an integration updated from before 2.0.0 onto the v2
        calls by itself, and it's removed along with the v1 calls.

        Runs once, as a background task of the config entry, each time the entry
        is set up. With no answer at all it tries again, waiting longer each
        time, until there's an answer, a link, or another stored push token. Any
        answer ends it, with or without a site, until the next setup.
        """
        push_token = self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)
        if not isinstance(push_token, str) or not push_token:
            return

        failures = 0
        while self._link_is_wanted(push_token):
            try:
                site_id = await fetch_site_id(self._hass, push_token)
            except BNGntControlBackendNoAnswerError:
                failures += 1
                retry_in = link_retry_interval(failures)
                _LOGGER.info(
                    "No answer about the stored push token's Zendo site, "
                    "asking again in %s seconds",
                    retry_in,
                )
                await asyncio.sleep(retry_in)
                continue
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Zendo didn't give a site for the stored push token (%s). An admin "
                    "can link Home Assistant by setting Zendo up from the app",
                    err,
                )
                return
            # Read again after the wait: writing the link also writes the push
            # token, which would replace a newer one.
            if self._link_is_wanted(push_token):
                self._store_link(site_id, push_token)
            return

    @callback
    def _link_is_wanted(self, push_token: str) -> bool:
        """Return whether there's no link, and ``push_token`` is still the stored one."""
        return (
            not self.is_linked
            and self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN) == push_token
        )

    @callback
    def _store_link(self, site_id: str, push_token: str) -> None:
        """Store the site and its push token together."""
        if self.site_id != site_id:
            _LOGGER.info("Linked to Zendo site %s", site_id)
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_LINKED_SITE_ID: site_id,
                CONF_PUSH_NOTIFICATION_TOKEN: push_token,
            },
        )

    # -- registrations, as v1_rules.py reads them -------------------------------

    @property
    def admin_registered(self) -> bool:
        return self.registrations.book.admin_registered

    def is_registered(self, profile_id: str, device_id: str) -> bool:
        return self.registrations.book.find_by_device(profile_id, device_id) is not None

    def is_revoked(self, profile_id: str, device_id: str) -> bool:
        return self.registrations.book.is_tombstoned(profile_id, device_id)

    def registration_for(self, profile_id: str, device_id: str) -> Registration | None:
        return self.registrations.book.find_by_device(profile_id, device_id)

    def registration_for_api_key(self, api_key: Any) -> Registration | None:
        return self.registrations.book.find_by_api_key(api_key)

    # -- tokens ---------------------------------------------------------------

    async def async_check_token(self, token: Any, site_id: str | None) -> TokenClaims:
        """Check a device control backend token. Raises ``_Refusal``."""
        keys = await self.keys.async_keys_for(token_kid(token))
        try:
            return check_token(token, keys, time.time(), site_id)
        except TokenError as err:
            raise _Refusal(err.code) from None

    def _linked_site_id(self) -> str:
        """Return the linked site. Raises ``_Refusal``."""
        site_id = self.site_id
        if site_id is None:
            raise _Refusal(ERROR_NOT_LINKED)
        return site_id

    async def async_admin_claims(self, token: Any) -> TokenClaims:
        """Check an admin call's token, for a registered device. Raises ``_Refusal``."""
        claims = await self.async_check_token(token, self._linked_site_id())
        if not claims.admin:
            raise _Refusal(ERROR_NOT_ADMIN)

        self._raise_if_revoked(claims)
        self._raise_if_unregistered(claims)
        return claims

    # -- registering and revoking ---------------------------------------------------

    async def async_register(self, token: Any, encryption: Any) -> dict[str, Any]:
        """Register the token's device. Raises ``_Refusal``."""
        claims = await self.async_check_token(token, self._linked_site_id())

        scheme = choose_scheme(encryption)
        if scheme is None:
            raise _Refusal(ERROR_UNSUPPORTED_SCHEME)

        # Synchronous from here: the tombstone is checked as the registration
        # is made, after every wait above. The public key is the token's.
        try:
            api_key, registration = self.registrations.book.register(
                claims.profile_id,
                claims.device_id,
                admin=claims.admin,
                public_key=claims.encryption_public_key,
                now=time.time(),
            )
        except RegistrationError as err:
            raise _Refusal(err.code) from None
        self.registrations.schedule_save()

        return {
            "success": True,
            "api_key": api_key,
            "encryption": {"scheme": registration.scheme, "key_id": registration.key_id},
        }

    async def async_revoke(
        self, profile_id: str, device_id: str, own_token_id: str | None
    ) -> str | None:
        """Revoke the device.

        Removes its registration and keeps its tombstone, tells it with
        ``bngnt_device_deleted``, ends its subscriptions, marks its managed
        device offline, and removes its long-lived tokens on any user, once the
        event has had time to go out. The caller's own token, ``own_token_id``,
        isn't removed here: its id is returned when it's one of the device's,
        for removing after the answer.
        """
        device = (profile_id, device_id)
        self.registrations.book.revoke(profile_id, device_id, time.time())
        self.registrations.schedule_save()

        data = {"profile_id": profile_id, "device_id": device_id}
        self.subscriptions.send_to_events(EVENT_DEVICE_DELETED, data)
        self._hass.bus.async_fire(EVENT_DEVICE_DELETED, data)
        self.subscriptions.end_for_device(device)

        registry: BNGntManagedDeviceRegistry | None = self._hass.data[DOMAIN].get(
            "managed_device_registry"
        )
        if registry is not None:
            registry.unregister_device(profile_id, device_id)

        site_id = self.site_id
        deferred: str | None = None
        if site_id is None:
            return deferred
        for user in await self._hass.auth.async_get_users():
            for refresh_token in list(user.refresh_tokens.values()):
                if refresh_token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    continue
                if not is_device_token_label(
                    refresh_token.client_name, site_id, profile_id, device_id
                ):
                    continue
                if refresh_token.id == own_token_id:
                    deferred = refresh_token.id
                    continue
                self.remove_token_later(refresh_token.id)
        return deferred

    @callback
    def remove_token_later(self, token_id: str) -> None:
        """Remove a token shortly, once what's queued on its connections has gone out."""

        @callback
        def _remove(_now: Any) -> None:
            refresh_token = self._hass.auth.async_get_refresh_token(token_id)
            if refresh_token is not None:
                self._hass.auth.async_remove_refresh_token(refresh_token)

        async_call_later(self._hass, TOKEN_REMOVAL_DELAY, _remove)

    # -- commands -------------------------------------------------------------

    @callback
    def send_command(self, registration: Registration, request: dict[str, Any]) -> None:
        """Send a JSON-RPC request to a registered device, in an envelope."""
        self.subscriptions.send_to_events(
            EVENT_MANAGED_DEVICE_COMMAND, build_command_envelope(request, registration)
        )

    # -- setting up ---------------------------------------------------------------

    async def async_setup_claims(self, token: Any) -> TokenClaims:
        """Check a setup call's token, against its own site. Raises ``_Refusal``.

        It needs no registration, since nothing can register before the link.
        """
        claims = await self.async_check_token(token, None)
        if not claims.admin:
            raise _Refusal(ERROR_NOT_ADMIN)
        self._raise_if_revoked(claims)
        return claims

    def _raise_if_revoked(self, claims: TokenClaims) -> None:
        if self.registrations.book.is_tombstoned(claims.profile_id, claims.device_id):
            raise _Refusal(ERROR_DEVICE_REVOKED)

    def _raise_if_unregistered(self, claims: TokenClaims) -> None:
        if self.registrations.book.find_by_device(claims.profile_id, claims.device_id) is None:
            raise _Refusal(ERROR_UNKNOWN_API_KEY)

    def setup_status(self, claims: TokenClaims) -> dict[str, Any]:
        """Answer how this integration stands for the site of a checked token."""
        return {
            "success": True,
            "version": INTEGRATION_VERSION,
            "link": site_link(self.site_id, claims.site_id),
            "push_token_serial": push_token_serial(
                self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)
            ),
        }

    async def async_set_up(
        self, user_is_admin: bool, token: Any, push_token: Any, destinations: Any
    ) -> dict[str, Any]:
        """Store a push token and its site, deep link destinations, or both.

        Answers as ``setup_status``, after the changes. Raises ``_Refusal`` and
        ``_InvalidField``, either way having changed nothing.
        """
        claims = await self.async_setup_claims(token)

        link = site_link(self.site_id, claims.site_id)
        if link == LINK_ANOTHER_PROPERTY:
            raise _Refusal(ERROR_WRONG_SITE)
        if link == LINK_NOT_LINKED:
            if not user_is_admin:
                raise _Refusal(ERROR_NOT_ADMIN)
            if push_token is None:
                raise _InvalidField("push_token")
        else:
            self._raise_if_unregistered(claims)

        if push_token is not None and (not isinstance(push_token, str) or not push_token):
            raise _InvalidField("push_token")
        if destinations is not None:
            destinations = _deep_link_destinations(destinations)

        if push_token is not None:
            try:
                push_site_id = await fetch_site_id(self._hass, push_token)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Couldn't confirm the push token's Zendo site", exc_info=True)
                raise _Refusal(ERROR_INVALID_PUSH_TOKEN) from None
            if push_site_id != claims.site_id:
                raise _Refusal(ERROR_WRONG_SITE)

            # Read again after the wait: another set up, or the link from a
            # stored push token, may have linked it, and the device may have been
            # revoked. Synchronous from here, so the link is read as it's written.
            self._raise_if_revoked(claims)
            if site_link(self.site_id, push_site_id) == LINK_ANOTHER_PROPERTY:
                raise _Refusal(ERROR_WRONG_SITE)
            self._store_link(push_site_id, push_token)
            async_dispatcher_send(self._hass, SIGNAL_CONFIG_UPDATED)

        if destinations is not None:
            self.store_deep_link_destinations(destinations)

        # With the token just stored, or the one already there. A refresh can be
        # rate limited, so one that fails doesn't fail the call.
        stored_push_token = self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)
        refresh_profiles = self._hass.data[DOMAIN].get("refresh_profiles")
        if stored_push_token and refresh_profiles is not None:
            try:
                await refresh_profiles(stored_push_token)
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("Couldn't refresh the profiles after setting up: %s", type(err).__name__)

        return self.setup_status(claims)

    # -- other admin calls ------------------------------------------------------

    @callback
    def store_deep_link_destinations(self, destinations: list[Any]) -> None:
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_CACHED_DEEP_LINK_DESTINATIONS: destinations},
        )


# ---------------------------------------------------------------------------
# WebSocket command helpers
# ---------------------------------------------------------------------------

def _schema(ws_type: str, fields: dict[Any, Any]) -> vol.All:
    """A command schema that drops unknown fields, as the v1 commands' do.

    Every field is taken as any value and checked in the handler, with fixed
    messages.
    """
    return vol.All(
        vol.Schema({vol.Required("type"): ws_type, **fields}, extra=vol.REMOVE_EXTRA)
    )


def _optional(*names: str) -> dict[Any, Any]:
    return {vol.Optional(name): object for name in names}


# The field every command that takes a device control backend token has it in.
_TOKEN_FIELD = "device_control_backend_token"


def _get_auth(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> BNGntDeviceAuth | None:
    auth: BNGntDeviceAuth | None = hass.data.get(DOMAIN, {}).get("device_auth")
    if auth is None:
        connection.send_error(
            msg["id"], ERROR_NOT_SET_UP, NOT_SET_UP_MESSAGE
        )
    return auth


def _get_registry(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> BNGntManagedDeviceRegistry | None:
    registry: BNGntManagedDeviceRegistry | None = hass.data.get(DOMAIN, {}).get(
        "managed_device_registry"
    )
    if registry is None:
        connection.send_error(
            msg["id"], ERROR_NOT_SET_UP, NOT_SET_UP_MESSAGE
        )
    return registry


def _refuse(connection: websocket_api.ActiveConnection, msg_id: int, code: str) -> None:
    connection.send_result(msg_id, {"success": False, "error": code})


def _invalid(connection: websocket_api.ActiveConnection, msg_id: int, err: Exception) -> None:
    connection.send_error(msg_id, websocket_api.ERR_INVALID_FORMAT, str(err))


def _string(msg: dict[str, Any], field: str, *, required: bool = True) -> str | None:
    value = msg.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise _InvalidField(field)
    return value


def _registration(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> tuple[BNGntDeviceAuth, BNGntManagedDeviceRegistry, Registration] | None:
    """Return the caller's registration, from its API key, or answer and return None."""
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return None
    registry = _get_registry(hass, connection, msg)
    if registry is None:
        return None
    registration = auth.registration_for_api_key(msg["api_key"])
    if registration is None:
        _refuse(connection, msg["id"], ERROR_UNKNOWN_API_KEY)
        return None
    return auth, registry, registration


def _ids(registration: Registration) -> tuple[str, str]:
    return (registration.profile_id, registration.device_id)


def _deep_link_destinations(value: Any) -> list[Any]:
    """Read deep link destinations: a list, or a JSON-stringified list.

    Raises ``_InvalidField``.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, list):
        raise _InvalidField("destinations")
    return value


# ---------------------------------------------------------------------------
# WebSocket commands -- no credential
# ---------------------------------------------------------------------------

@websocket_api.websocket_command(_schema(f"{WS_PREFIX}info", {}))
@callback
def ws_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the integration's version, and whether it's linked to a site."""
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    connection.send_result(
        msg["id"], {"version": INTEGRATION_VERSION, "linked": auth.is_linked}
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}register_device",
        {vol.Required(_TOKEN_FIELD): object, **_optional("encryption")},
    )
)
@websocket_api.async_response
async def ws_register_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Register the device a device control backend token names."""
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    try:
        result = await auth.async_register(msg[_TOKEN_FIELD], msg.get("encryption"))
    except _Refusal as err:
        _refuse(connection, msg["id"], err.code)
        return
    connection.send_result(msg["id"], result)


# ---------------------------------------------------------------------------
# WebSocket commands -- the API key (a device acting for itself)
# ---------------------------------------------------------------------------

_API_KEY = {vol.Required("api_key"): object}


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}subscribe_events", _API_KEY)
)
@callback
def ws_subscribe_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the device the commands for registered devices and the events for all.

    Fails with the error code ``unknown_api_key`` for an API key that isn't one.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    registration = auth.registration_for_api_key(msg["api_key"])
    if registration is None:
        connection.send_error(msg["id"], ERROR_UNKNOWN_API_KEY, "Unknown API key")
        return
    auth.subscriptions.add_events(connection, msg["id"], _ids(registration))
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}register_managed_device",
        {**_API_KEY, **_optional(*DEVICE_INFO_FIELD_NAMES)},
    )
)
@callback
def ws_register_managed_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Register or update the caller's managed device."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found

    info = {field: msg[field] for field in DEVICE_INFO_FIELD_NAMES if field in msg}
    try:
        info = REGISTER_DEVICE_INFO_SCHEMA(info)
    except vol.Invalid:
        _invalid(connection, msg["id"], _InvalidField("device info"))
        return

    connection.send_result(
        msg["id"], register_managed_device(registry, *_ids(registration), info)
    )


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}unregister_managed_device", _API_KEY)
)
@callback
def ws_unregister_managed_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Mark the caller's managed device offline, as unregistered."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    connection.send_result(
        msg["id"], unregister_managed_device(registry, *_ids(registration))
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}update_managed_device_state",
        {**_API_KEY, vol.Required("state"): object},
    )
)
@callback
def ws_update_managed_device_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the caller's state: an object, or a JSON-stringified object."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    try:
        state = parse_state(msg["state"])
    except Exception:  # noqa: BLE001
        _invalid(connection, msg["id"], _InvalidField("state"))
        return
    connection.send_result(
        msg["id"], update_managed_device_state(registry, *_ids(registration), state)
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}managed_device_command_response",
        {**_API_KEY, vol.Required("response"): object},
    )
)
@callback
def ws_managed_device_command_response(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the caller's answer to a command: an object, or a JSON string."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    response = parse_command_response(msg["response"])
    if response is None:
        _invalid(connection, msg["id"], _InvalidField("response"))
        return
    registry.handle_command_response(
        response, _ids(registration), registered_responder=True
    )
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}report_managed_device_admin_code",
        {**_API_KEY, vol.Required("admin_code"): object},
    )
)
@callback
def ws_report_managed_device_admin_code(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Keep the admin code the caller reports, or that it has none."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    code = msg["admin_code"]
    if code is not None and not is_valid_admin_code(code):
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, INVALID_REPORTED_ADMIN_CODE_MESSAGE
        )
        return
    connection.send_result(
        msg["id"], report_admin_code(registry, *_ids(registration), code)
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}report_managed_device_settings_backup",
        {**_API_KEY, vol.Required("settings_backup"): object},
    )
)
@callback
def ws_report_managed_device_settings_backup(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Keep the settings backup the caller reports."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    settings_backup = msg["settings_backup"]
    if not is_valid_settings_backup(settings_backup):
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, INVALID_SETTINGS_BACKUP_MESSAGE
        )
        return
    connection.send_result(
        msg["id"], report_settings_backup(registry, *_ids(registration), settings_backup)
    )


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}get_managed_device_settings_restore", _API_KEY)
)
@callback
def ws_get_managed_device_settings_restore(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the restore pending on the caller's record, or null for none.

    The restore's ``admin_code`` comes sealed to the device's public key.
    """
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    connection.send_result(
        msg["id"],
        seal_settings_restore(settings_restore(registry, *_ids(registration)), registration),
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}complete_managed_device_settings_restore",
        {**_API_KEY, **_optional("restore_id")},
    )
)
@callback
def ws_complete_managed_device_settings_restore(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Drop the caller's record's restore once it has applied it."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    _, registry, registration = found
    try:
        restore_id = _string(msg, "restore_id")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    connection.send_result(
        msg["id"], complete_settings_restore(registry, *_ids(registration), restore_id)
    )


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}doorbell_action", {**_API_KEY, **_optional("event_id", "action")})
)
@callback
def ws_doorbell_action(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Pass a doorbell action on to every device, as ``doorbell_action`` does."""
    found = _registration(hass, connection, msg)
    if found is None:
        return
    try:
        event_id = _string(msg, "event_id")
        action = msg.get("action")
        if action is not None and not isinstance(action, str):
            raise _InvalidField("action")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return

    # ``action`` is an unconstrained string: the app ignores one it doesn't know.
    payload: dict[str, Any] = {"eventId": event_id}
    if action is not None:
        payload["action"] = action
    hass.bus.async_fire(EVENT_DOORBELL_ACTION, {"payload": json.dumps(payload)})
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}get_encryption_key", _API_KEY)
)
@callback
def ws_get_encryption_key(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the site config encryption key, sealed to the caller's public key.

    With none held, the answer is ``key: null``, in clear.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    registration = auth.registration_for_api_key(msg["api_key"])
    if registration is None:
        _refuse(connection, msg["id"], ERROR_UNKNOWN_API_KEY)
        return
    connection.send_result(
        msg["id"], encryption_key_answer(auth.encryption_key.held(), registration)
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}revoke_device",
        _optional("api_key", _TOKEN_FIELD, "profile_id", "device_id"),
    )
)
@websocket_api.async_response
async def ws_revoke_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Revoke a device.

    With ``api_key``: the caller leaves the site. With
    ``device_control_backend_token``, ``profile_id`` and ``device_id``: an admin's
    device revokes another device.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return

    if "api_key" in msg:
        registration = auth.registration_for_api_key(msg["api_key"])
        if registration is None:
            _refuse(connection, msg["id"], ERROR_UNKNOWN_API_KEY)
            return
        target = _ids(registration)
    else:
        try:
            await auth.async_admin_claims(msg.get(_TOKEN_FIELD))
            target = (_string(msg, "profile_id"), _string(msg, "device_id"))
        except _Refusal as err:
            _refuse(connection, msg["id"], err.code)
            return
        except _InvalidField as err:
            _invalid(connection, msg["id"], err)
            return

    deferred = await auth.async_revoke(*target, connection.refresh_token_id)
    connection.send_result(msg["id"], {"success": True})
    if deferred is not None:
        auth.remove_token_later(deferred)


# ---------------------------------------------------------------------------
# WebSocket commands -- the admin-scoped token
# ---------------------------------------------------------------------------

_TOKEN = {vol.Optional(_TOKEN_FIELD): object}


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}subscribe_managed_device_changes", _TOKEN)
)
@websocket_api.async_response
async def ws_subscribe_managed_device_changes(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the admin's device ``bngnt_managed_device_updated``, ``_removed`` and
    ``_replaced``.

    Fails with the refusal's code as the error code.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    try:
        claims = await auth.async_admin_claims(msg.get(_TOKEN_FIELD))
    except _Refusal as err:
        connection.send_error(msg["id"], err.code, "Refused")
        return
    auth.subscriptions.add_changes(
        connection, msg["id"], (claims.profile_id, claims.device_id)
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(_schema(f"{WS_PREFIX}setup_status", _TOKEN))
@websocket_api.async_response
async def ws_setup_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the version, the link and the stored push token's serial.

    Calls no cloud, and needs no registration, so an admin's device can ask before
    anything is set up.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    try:
        claims = await auth.async_setup_claims(msg.get(_TOKEN_FIELD))
    except _Refusal as err:
        _refuse(connection, msg["id"], err.code)
        return
    connection.send_result(msg["id"], auth.setup_status(claims))


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}set_up", {**_TOKEN, **_optional("push_token", "destinations")})
)
@websocket_api.async_response
async def ws_set_up(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store a push token and its site, deep link destinations, or both."""
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    try:
        result = await auth.async_set_up(
            connection.user.is_admin,
            msg.get(_TOKEN_FIELD),
            msg.get("push_token"),
            msg.get("destinations"),
        )
    except _Refusal as err:
        _refuse(connection, msg["id"], err.code)
        return
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


async def _admin_registry(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> tuple[BNGntDeviceAuth, BNGntManagedDeviceRegistry, TokenClaims] | None:
    """Check an admin call, or answer and return None.

    Returns the token's claims too: the caller's ids and encryption public key.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return None
    registry = _get_registry(hass, connection, msg)
    if registry is None:
        return None
    try:
        claims = await auth.async_admin_claims(msg.get(_TOKEN_FIELD))
    except _Refusal as err:
        _refuse(connection, msg["id"], err.code)
        return None
    return auth, registry, claims


@websocket_api.websocket_command(_schema(f"{WS_PREFIX}list_managed_devices", _TOKEN))
@websocket_api.async_response
async def ws_list_managed_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List every managed device, each as ``bngnt_managed_device_updated`` carries it."""
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    _, registry, _ = checked
    connection.send_result(
        msg["id"], {"success": True, "managed_devices": list_managed_devices(registry)}
    )


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}replace_managed_device",
        {**_TOKEN, **_optional("profile_id", "device_id", "managed_device_id")},
    )
)
@websocket_api.async_response
async def ws_replace_managed_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Attach the replacement, the device the ids name, to another record."""
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    _, registry, _ = checked
    try:
        profile_id = _string(msg, "profile_id")
        device_id = _string(msg, "device_id")
        managed_device_id = _string(msg, "managed_device_id")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    connection.send_result(
        msg["id"],
        replace_managed_device(registry, profile_id, device_id, managed_device_id),
    )


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}delete_managed_device", {**_TOKEN, **_optional("managed_device_id")})
)
@websocket_api.async_response
async def ws_delete_managed_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Delete an offline managed device and its Home Assistant device."""
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    _, registry, _ = checked
    try:
        managed_device_id = _string(msg, "managed_device_id")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], delete_managed_device(registry, managed_device_id))


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}get_managed_device_admin_code",
        {**_TOKEN, **_optional("managed_device_id")},
    )
)
@websocket_api.async_response
async def ws_get_managed_device_admin_code(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return a managed device's admin code, as its device last reported it.

    Reads only the store, so it works while the device is offline. A record whose
    attached device hasn't registered reads as unknown. The code, or null, comes
    sealed to the encryption public key in the caller's token.
    """
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    auth, registry, claims = checked
    try:
        token_public_key(claims)
    except TokenError as err:
        _refuse(connection, msg["id"], err.code)
        return
    try:
        managed_device_id = _string(msg, "managed_device_id")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return

    lookup = admin_code_lookup(registry, managed_device_id)
    device = registry.get_device(managed_device_id)
    if lookup is None or device is None:
        _refuse(connection, msg["id"], ERROR_TARGET_NOT_FOUND)
        return
    lookup = admin_code_lookup_for_attached_device(
        lookup, auth.registrations.book, device["profile_id"], device["device_id"]
    )
    connection.send_result(
        msg["id"], seal_admin_code_answer({"success": True, **lookup}, claims)
    )


CODE_OR_REMOVE_MESSAGE = "Give either code or remove: true"
INVALID_CODE_MESSAGE = "code must be exactly 4, 6 or 8 digits"


def _admin_code_to_configure(msg: dict[str, Any]) -> str | None:
    """Return the code ``code`` sets, or None for ``remove: true``.

    Exactly one of them. Raises ``_InvalidRequest``, whose message is fixed
    and never has the code.
    """
    remove = msg.get("remove")
    if remove is not None and not isinstance(remove, bool):
        raise _InvalidField("remove")
    code = msg.get("code")
    if (code is None) == (remove is not True):
        raise _InvalidRequest(CODE_OR_REMOVE_MESSAGE)
    if code is None:
        return None
    if not is_valid_admin_code(code):
        raise _InvalidRequest(INVALID_CODE_MESSAGE)
    return code


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}configure_managed_device_admin_code",
        {**_TOKEN, **_optional("managed_device_id", "code", "remove")},
    )
)
@websocket_api.async_response
async def ws_configure_managed_device_admin_code(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Set or remove the admin code on a managed device.

    Sends its device ``adminCode.set`` and waits for the answer, as
    ``managed_device_admin_code_configure`` does for each device.
    """
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    _, registry, _ = checked
    try:
        managed_device_id = _string(msg, "managed_device_id")
        code = _admin_code_to_configure(msg)
    except _InvalidRequest as err:
        _invalid(connection, msg["id"], err)
        return

    failure = await async_send_admin_code(registry, managed_device_id, code)
    if failure is not None:
        _refuse(connection, msg["id"], failure.error)
        return
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}set_deep_link_destinations", {**_TOKEN, **_optional("destinations")})
)
@websocket_api.async_response
async def ws_set_deep_link_destinations(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store the deep link destinations: a list, or a JSON-stringified list."""
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    auth, _, _ = checked
    try:
        destinations = _deep_link_destinations(msg.get("destinations"))
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    auth.store_deep_link_destinations(destinations)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    _schema(
        f"{WS_PREFIX}notify_site_config_manifest_update",
        {**_TOKEN, **_optional("payload")},
    )
)
@websocket_api.async_response
async def ws_notify_site_config_manifest_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Tell every device that the site config changed."""
    checked = await _admin_registry(hass, connection, msg)
    if checked is None:
        return
    try:
        payload = _string(msg, "payload")
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return
    hass.bus.async_fire(EVENT_SITE_CONFIG_MANIFEST_UPDATED, {"payload": payload})
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    _schema(f"{WS_PREFIX}claim_encryption_key", {**_TOKEN, **_optional("key")})
)
@websocket_api.async_response
async def ws_claim_encryption_key(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store ``key`` if no site config encryption key is held, and answer with
    the one kept, sealed to the encryption public key in the caller's token.
    """
    auth = _get_auth(hass, connection, msg)
    if auth is None:
        return
    try:
        claims = await auth.async_admin_claims(msg.get(_TOKEN_FIELD))
        token_public_key(claims)
        key = _string(msg, "key")
        # What a helper with no value reads as, never a key.
        if helper_encryption_key(key) is None:
            raise _InvalidField("key")
    except (_Refusal, TokenError) as err:
        _refuse(connection, msg["id"], err.code)
        return
    except _InvalidField as err:
        _invalid(connection, msg["id"], err)
        return

    # The token check can span an unload, and a closed store writes nothing.
    if auth.encryption_key.closed:
        connection.send_error(msg["id"], ERROR_NOT_SET_UP, NOT_SET_UP_MESSAGE)
        return
    connection.send_result(
        msg["id"], claimed_encryption_key_answer(auth.encryption_key.claim(key), claims)
    )


_COMMANDS = (
    ws_info,
    ws_register_device,
    ws_subscribe_events,
    ws_register_managed_device,
    ws_unregister_managed_device,
    ws_update_managed_device_state,
    ws_managed_device_command_response,
    ws_report_managed_device_admin_code,
    ws_report_managed_device_settings_backup,
    ws_get_managed_device_settings_restore,
    ws_complete_managed_device_settings_restore,
    ws_doorbell_action,
    ws_get_encryption_key,
    ws_revoke_device,
    ws_subscribe_managed_device_changes,
    ws_setup_status,
    ws_set_up,
    ws_list_managed_devices,
    ws_replace_managed_device,
    ws_delete_managed_device,
    ws_get_managed_device_admin_code,
    ws_configure_managed_device_admin_code,
    ws_set_deep_link_destinations,
    ws_notify_site_config_manifest_update,
    ws_claim_encryption_key,
)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the ``zendo/v2/`` WebSocket commands.

    A WebSocket command can't be unregistered: after an unload, each answers
    with an error until the entry is set up again.
    """
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)
