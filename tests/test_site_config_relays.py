"""Tests for the site config relays in custom_components/zendo/device_api.py:
``zendo/v2/notify_site_config_manifest_update`` and
``zendo/v2/notify_site_config_restore``. Each relay is checked the same way.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs PyJWT and cryptography, as test_device_auth.py does, and nothing from Home
Assistant: the package's modules are loaded from their files, without its
``__init__.py``, and every Home Assistant, voluptuous and aiohttp name they
import is a stand-in. The few that the relays and the subscriptions use when
they run are given the behaviour Home Assistant's have, in ``_OVERRIDES``. The
admin check is the real one, with the shared vectors' tokens.
"""

from __future__ import annotations

import base64
import importlib
import importlib.abc
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "custom_components" / "zendo"
PACKAGE = "bngnt_relays_package"
VECTORS = json.loads(
    (Path(__file__).resolve().parent / "companion-app-authentication-vectors.json").read_text()
)

NOW = VECTORS["now"]
SITE_ID = VECTORS["linkedSiteId"]
DEVICE = VECTORS["device"]

# Every module under these names is a stand-in.
_STAND_IN_ROOTS = frozenset({"homeassistant", "voluptuous", "aiohttp"})


class _StandInType(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _stand_in(name)


class _StandIn(metaclass=_StandInType):
    """Anything a module does with an imported name as it loads: subclass it,
    call it, read from it or subscript it."""

    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _stand_in(name)

    def __call__(self, *args, **kwargs):
        return _StandIn()


def _stand_in(name):
    return _StandInType(name, (_StandIn,), {})


class _Marker(str):
    """``vol.Required`` and ``vol.Optional``: equal to the key they mark, as
    voluptuous's are."""

    def __new__(cls, schema, *args, **kwargs):
        return super().__new__(cls, schema)


class _Schema(_StandIn):
    def __init__(self, schema=None, *args, **kwargs):
        self.schema = schema


class _All(_StandIn):
    def __init__(self, *validators, **kwargs):
        self.validators = validators


def _websocket_command(schema):
    """Read the command's type from its schema, as Home Assistant's does."""
    if isinstance(schema, dict):
        command = schema["type"]
    else:
        command = schema.validators[0].schema["type"]

    def decorate(func):
        func._ws_command = command
        func._ws_schema = schema
        return func

    return decorate


def _event_message(iden, event):
    return {"id": iden, "type": "event", "event": event}


def _unchanged(func):
    return func


class _HomeAssistantError(Exception):
    pass


class _ServiceValidationError(_HomeAssistantError):
    pass


# ``async_response`` leaves the handler a coroutine function, for the tests to await.
_OVERRIDES = {
    "homeassistant.core": {"callback": _unchanged},
    "homeassistant.components.websocket_api": {
        "websocket_command": _websocket_command,
        "async_response": _unchanged,
        "event_message": _event_message,
        "ERR_INVALID_FORMAT": "invalid_format",
    },
    "homeassistant.exceptions": {
        "HomeAssistantError": _HomeAssistantError,
        "ServiceValidationError": _ServiceValidationError,
    },
    "voluptuous": {"Schema": _Schema, "All": _All, "Required": _Marker, "Optional": _Marker},
}


class _StandInModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fullname = f"{self.__name__}.{name}"
        if fullname in _OVERRIDES:
            return importlib.import_module(fullname)
        return _stand_in(name)


class _StandInFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] not in _STAND_IN_ROOTS:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=True)

    def create_module(self, spec):
        return _StandInModule(spec.name)

    def exec_module(self, module):
        module.__dict__.update(_OVERRIDES.get(module.__name__, {}))


def _load_package():
    """Load device_api.py and what it imports, and leave ``sys.modules`` as it was."""
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PACKAGE_DIR)]
    finder = _StandInFinder()
    with patch.dict(sys.modules):
        for name in list(sys.modules):
            if name.partition(".")[0] in _STAND_IN_ROOTS:
                del sys.modules[name]
        sys.modules[PACKAGE] = package
        sys.meta_path.insert(0, finder)
        try:
            importlib.import_module(f"{PACKAGE}.device_api")
        finally:
            sys.meta_path.remove(finder)
    return package


_package = _load_package()
device_api = _package.device_api
device_auth = _package.device_auth

RELAYS = (
    ("zendo/v2/notify_site_config_manifest_update", "bngnt_site_config_manifest_updated"),
    ("zendo/v2/notify_site_config_restore", "bngnt_site_config_restore"),
)

PAYLOAD = '{"jobId":"job_1","status":"running"}'


def _token(name):
    return next(v for v in VECTORS["tokens"] if v["name"] == name)["token"]


def _handler(command):
    return next(h for h in device_api._COMMANDS if h._ws_command == command)


def _new_public_key():
    der = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return base64.b64encode(der).decode()


class _Bus:
    """Calls each listener as the event fires, as Home Assistant does a callback's."""

    def __init__(self):
        self.fired = []
        self._listeners = {}

    def async_listen(self, event_type, listener):
        self._listeners.setdefault(event_type, []).append(listener)
        return lambda: self._listeners[event_type].remove(listener)

    def async_fire(self, event_type, data):
        self.fired.append((event_type, data))
        event = SimpleNamespace(event_type=event_type, data=data)
        for listener in list(self._listeners.get(event_type, ())):
            listener(event)


class _Connection:
    def __init__(self):
        self.results = []
        self.errors = []
        self.messages = []
        self.subscriptions = {}

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))

    def send_message(self, message):
        self.messages.append(message)


class _Keys:
    """The signing keys from the vectors' record, with no DNS read."""

    async def async_keys_for(self, kid):
        return device_auth.parse_keys_record(VECTORS["keysRecord"]["content"]).keys


class _Registrations:
    def __init__(self):
        self.book = device_auth.RegistrationBook()

    def schedule_save(self):
        pass


class SiteConfigRelayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = _Bus()
        self.hass = SimpleNamespace(bus=self.bus, data={})
        entry = SimpleNamespace(data={device_api.CONF_LINKED_SITE_ID: SITE_ID})
        self.auth = device_api.BNGntDeviceAuth(
            self.hass, entry, _Keys(), _Registrations(), None
        )
        self.hass.data[device_api.DOMAIN] = {
            "device_auth": self.auth,
            "managed_device_registry": object(),
        }
        self.admin_api_key, _ = self.auth.registrations.book.register(
            DEVICE["profileId"],
            DEVICE["deviceId"],
            admin=True,
            public_key=DEVICE["encryptionKey"]["publicKeySpkiBase64"],
            now=NOW,
        )
        self.auth.start()
        clock = patch.object(device_api, "time", SimpleNamespace(time=lambda: float(NOW)))
        clock.start()
        self.addCleanup(clock.stop)

    async def _relay(self, command, token_name="valid-admin", **fields):
        connection = _Connection()
        msg = {
            "id": 1,
            "type": command,
            "device_control_backend_token": _token(token_name),
            **fields,
        }
        await _handler(command)(self.hass, connection, msg)
        return connection

    def _subscribe_events(self, api_key, msg_id):
        connection = _Connection()
        handler = _handler("zendo/v2/subscribe_events")
        handler(
            self.hass,
            connection,
            {"id": msg_id, "type": "zendo/v2/subscribe_events", "api_key": api_key},
        )
        self.assertEqual(connection.results, [(msg_id, None)])
        return connection

    def test_registered_with_the_token_and_the_payload(self):
        for command, _ in RELAYS:
            with self.subTest(command):
                schema = _handler(command)._ws_schema.validators[0].schema
                self.assertEqual(schema["type"], command)
                self.assertEqual(
                    set(schema), {"type", "device_control_backend_token", "payload"}
                )

    async def test_an_admin_is_answered_and_the_event_fires(self):
        for command, event_type in RELAYS:
            with self.subTest(command):
                self.bus.fired.clear()
                connection = await self._relay(command, payload=PAYLOAD)
                self.assertEqual(connection.results, [(1, {"success": True})])
                self.assertEqual(connection.errors, [])
                self.assertEqual(self.bus.fired, [(event_type, {"payload": PAYLOAD})])

    async def test_a_token_without_the_admin_scope_is_refused(self):
        for command, _ in RELAYS:
            with self.subTest(command):
                connection = await self._relay(command, "valid-plain", payload=PAYLOAD)
                self.assertEqual(
                    connection.results, [(1, {"success": False, "error": "not_admin"})]
                )
                self.assertEqual(self.bus.fired, [])

    async def test_the_payload_is_passed_on_unchanged(self):
        payloads = (
            PAYLOAD,
            '{"jobId":"job_1","status":"failed","rolledBack":true}',
            '{"status":"a status the hub has never heard of","extra":[1,2,3]}',
            "not JSON at all",
            "  été\n",
        )
        for command, event_type in RELAYS:
            for payload in payloads:
                with self.subTest(command=command, payload=payload):
                    self.bus.fired.clear()
                    await self._relay(command, payload=payload)
                    [(fired_type, data)] = self.bus.fired
                    self.assertEqual(fired_type, event_type)
                    self.assertEqual(data, {"payload": payload})
                    self.assertIs(data["payload"], payload)

    async def test_a_payload_that_isnt_a_string_is_refused(self):
        for command, _ in RELAYS:
            for fields in (
                {},
                {"payload": None},
                {"payload": ""},
                {"payload": {"jobId": "job_1", "status": "running"}},
                {"payload": ["running"]},
                {"payload": 1},
            ):
                with self.subTest(command=command, fields=fields):
                    connection = await self._relay(command, **fields)
                    self.assertEqual(
                        connection.errors, [(1, "invalid_format", "Invalid payload")]
                    )
                    self.assertEqual(connection.results, [])
                    self.assertEqual(self.bus.fired, [])

    async def test_every_events_subscription_hears_it(self):
        other_api_key, _ = self.auth.registrations.book.register(
            "profile_other",
            "device_other",
            admin=False,
            public_key=_new_public_key(),
            now=NOW,
        )
        for command, event_type in RELAYS:
            with self.subTest(command):
                admin = self._subscribe_events(self.admin_api_key, 10)
                other = self._subscribe_events(other_api_key, 20)
                changes = _Connection()
                self.auth.subscriptions.add_changes(
                    changes, 30, (DEVICE["profileId"], DEVICE["deviceId"])
                )

                await self._relay(command, payload=PAYLOAD)

                for connection, msg_id in ((admin, 10), (other, 20)):
                    self.assertEqual(
                        connection.messages,
                        [
                            _event_message(
                                msg_id,
                                {"event_type": event_type, "data": {"payload": PAYLOAD}},
                            )
                        ],
                    )
                self.assertEqual(changes.messages, [])
                for connection in (admin, other, changes):
                    for unsubscribe in connection.subscriptions.values():
                        unsubscribe()


if __name__ == "__main__":
    unittest.main()
