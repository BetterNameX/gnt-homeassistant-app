"""Tests for language and region in custom_components/zendo/managed_devices.py:
the ``managed_device_language_region_configure`` action, the ``locale.set``
command it sends, and the five sensors made from the state.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs PyJWT, cryptography and voluptuous, all of which Home Assistant ships.
The package's modules are loaded from their files, without its
``__init__.py``, and every Home Assistant name they import is a stand-in. The
schema is the real voluptuous one, with copies of Home Assistant's
``cv.string`` and ``cv.has_at_least_one_key``, so it accepts and refuses what
Home Assistant does. The registry is the real one, with a stand-in device on
the event bus that answers each command.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import importlib.abc
import importlib.util
import json
import sys
import types
import unittest
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import voluptuous as vol

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "custom_components" / "zendo"
PACKAGE = "bngnt_language_region_package"

# Every module under this name is a stand-in.
_STAND_IN_ROOTS = frozenset({"homeassistant"})


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


def _unchanged(func):
    return func


class _HomeAssistantError(Exception):
    pass


class _ServiceValidationError(_HomeAssistantError):
    pass


def _cv_string(value):
    """Home Assistant's ``cv.string``, less its template result types."""
    if value is None:
        raise vol.Invalid("string value is None")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        raise vol.Invalid("value should be a string")
    return str(value)


def _has_at_least_one_key(*keys):
    """Home Assistant's ``cv.has_at_least_one_key``."""
    key_set = set(keys)

    def validate(obj):
        if not isinstance(obj, dict):
            raise vol.Invalid("expected dictionary")
        if not key_set.isdisjoint(obj):
            return obj
        expected = ", ".join(str(k) for k in keys)
        raise vol.Invalid(f"must contain at least one of {expected}.")

    return validate


class _SensorDeviceClass(str, enum.Enum):
    BATTERY = "battery"
    ENUM = "enum"


class _SensorStateClass(str, enum.Enum):
    MEASUREMENT = "measurement"


class _Entity:
    hass = None

    def async_write_ha_state(self):
        pass

    def async_entity_id_changed(self, old_entity_id):
        pass


_OVERRIDES = {
    "homeassistant.core": {"callback": _unchanged},
    "homeassistant.exceptions": {
        "HomeAssistantError": _HomeAssistantError,
        "ServiceValidationError": _ServiceValidationError,
    },
    "homeassistant.helpers.config_validation": {
        "string": _cv_string,
        "has_at_least_one_key": _has_at_least_one_key,
    },
    "homeassistant.helpers.device_registry": {
        "async_get": lambda hass: hass.device_registry,
    },
    "homeassistant.components.sensor": {
        "SensorDeviceClass": _SensorDeviceClass,
        "SensorStateClass": _SensorStateClass,
        "SensorEntity": _Entity,
    },
    "homeassistant.components.binary_sensor": {"BinarySensorEntity": _Entity},
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
    """Load managed_devices.py and what it imports, and leave ``sys.modules`` as it was."""
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
            importlib.import_module(f"{PACKAGE}.managed_devices")
        finally:
            sys.meta_path.remove(finder)
    return package


_package = _load_package()
managed_devices = _package.managed_devices
status_history = _package.status_history

SCHEMA = managed_devices.SERVICE_MANAGED_DEVICE_LANGUAGE_REGION_CONFIGURE_SCHEMA
DOMAIN = managed_devices.DOMAIN

STRINGS = json.loads((PACKAGE_DIR / "strings.json").read_text())

FIELDS = (
    "language",
    "temperature_unit",
    "measurement_system",
    "clock",
    "first_weekday",
)

# The tags the action's select lists: the app's languages when it shipped.
LANGUAGES = ("en-GB", "en-US", "de-DE", "es-ES", "fr-FR", "it-IT", "nl-NL")

REGION_VALUES = {
    "temperature_unit": ("celsius", "fahrenheit"),
    "measurement_system": ("metric", "uk", "us"),
    "clock": ("12_hour", "24_hour"),
    "first_weekday": ("monday", "sunday"),
}

# What a device on an app with language and region reports.
REPORTED = {
    "language": "it-IT",
    "temperature_unit": "celsius",
    "measurement_system": "metric",
    "clock": "24_hour",
    "first_weekday": "monday",
}

BASE_STATE = {"battery_level": 80, "screen_state": "active", "locked": False}

# The action's ``device`` field, naming one Home Assistant device.
SELECTED = {"device": ["ha_device_1"]}

FRONT_DESK = ("profile_1", "device_front_desk", "Front desk", "md_front_desk")
MEETING_ROOM = ("profile_1", "device_meeting_room", "Meeting room", "md_meeting_room")
GUEST_ROOM = ("profile_2", "device_guest_room", "Guest room", "md_guest_room")


def _error(code, message):
    return {"error": {"code": code, "message": message}}


class _DeviceRegistry:
    """Home Assistant's device registry, as the managed device registry uses it."""

    def __init__(self):
        self._devices = {}

    def async_get_or_create(self, *, config_entry_id, identifiers, **fields):
        for device in self._devices.values():
            if device.identifiers == identifiers:
                return device
        device = SimpleNamespace(
            id=f"ha_device_{len(self._devices) + 1}",
            identifiers=set(identifiers),
            name_by_user=None,
        )
        self._devices[device.id] = device
        return device

    def async_update_device(self, device_id, **fields):
        pass

    def async_get(self, device_id):
        return self._devices.get(device_id)

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return next(
            (d for d in self._devices.values() if identifier in d.identifiers), None
        )


class _Bus:
    """The event bus, with the Zendo devices listening for their commands.

    A device answers each command it's sent as soon as it gets it: with its
    entry in ``answers``, or success.
    """

    def __init__(self):
        self.registry = None
        self.sent = []
        self.answers = {}

    def async_fire(self, event_type, data):
        if event_type != managed_devices.EVENT_MANAGED_DEVICE_COMMAND:
            return
        ids = (data["profile_id"], data["device_id"])
        request = json.loads(data["request"])
        self.sent.append((ids[1], request["method"], request["params"]))
        answer = self.answers.get(ids[1], {"result": {"success": True}})
        self.registry.handle_command_response(
            {"jsonrpc": "2.0", "id": request["id"], **answer},
            ids,
            registered_responder=False,
        )


class _Hass:
    def __init__(self, loop=None):
        self.loop = loop
        self.bus = _Bus()
        self.device_registry = _DeviceRegistry()
        self.config_entries = SimpleNamespace(async_entries=lambda domain: [])
        self.added_sensors = []
        self.data = {
            DOMAIN: {"async_add_managed_device_sensors": self._add_sensors},
        }

    def _add_sensors(self, entities, update_before_add=False):
        self.added_sensors.extend(entities)


def _make_registry(hass):
    registry = managed_devices.BNGntManagedDeviceRegistry(
        hass,
        SimpleNamespace(entry_id="entry_1", data={}),
        SimpleNamespace(status=lambda key: "unknown"),
        None,
        None,
        SimpleNamespace(
            history=status_history.BNGntStatusHistory(lambda: UTC, 180)
        ),
    )
    hass.data[DOMAIN]["managed_device_registry"] = registry
    hass.bus.registry = registry
    return registry


def _register(registry, device):
    profile_id, device_id, name, hint = device
    return registry.register_device(
        profile_id=profile_id,
        device_id=device_id,
        device_name=name,
        device_brand=None,
        device_type="tablet",
        device_model_name=None,
        device_model_slug=None,
        device_launcher_id=None,
        device_serial_number=None,
        device_os="android",
        device_os_version=None,
        app_version="26.11.24",
        managed_device_id=hint,
    )


def _report(registry, device, state):
    registry.update_state(profile_id=device[0], device_id=device[1], state=state)


def _sensor_keys(entities):
    """The property key of each sensor, from its unique ID ``md_<key>_<property>``."""
    return {
        entity._attr_unique_id.removeprefix(f"md_{entity._key}_") for entity in entities
    }


class SchemaTest(unittest.TestCase):
    def _valid(self, **fields):
        return SCHEMA({**SELECTED, **fields})

    def _refused(self, **fields):
        with self.assertRaises(vol.Invalid):
            SCHEMA({**SELECTED, **fields})

    def test_the_fields(self):
        fields = {str(marker) for marker in SCHEMA.validators[0].schema}
        self.assertEqual(fields, {"device", *FIELDS})
        self.assertEqual(set(managed_devices.LANGUAGE_REGION_PARAMS), set(FIELDS))

    def test_any_one_setting_is_enough(self):
        values = {"language": "it-IT", **{f: v[0] for f, v in REGION_VALUES.items()}}
        for field, value in values.items():
            with self.subTest(field):
                self.assertEqual(self._valid(**{field: value}), {**SELECTED, field: value})

    def test_no_setting_is_refused(self):
        with self.assertRaisesRegex(vol.Invalid, "at least one of language"):
            SCHEMA(dict(SELECTED))

    def test_a_device_is_required(self):
        with self.assertRaises(vol.Invalid):
            SCHEMA({"language": "it-IT"})

    def test_all_five_together(self):
        fields = {"language": "automatic", **{f: "automatic" for f in REGION_VALUES}}
        self.assertEqual(self._valid(**fields), {**SELECTED, **fields})

    def test_each_region_setting_takes_its_values_and_automatic(self):
        for field, values in REGION_VALUES.items():
            for value in ("automatic", *values):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self._valid(**{field: value})[field], value)

    def test_each_region_setting_refuses_anything_else(self):
        others = ("", "Celsius", "AUTOMATIC", "kelvin", "imperial", "24h", "24-hour",
                  "saturday", None, True, 24)
        for field in REGION_VALUES:
            for value in others:
                with self.subTest(field=field, value=value):
                    self._refused(**{field: value})

    def test_a_region_value_of_another_setting_is_refused(self):
        self._refused(clock="monday")
        self._refused(first_weekday="metric")
        self._refused(temperature_unit="us")

    def test_the_language_takes_any_tag(self):
        # The device is the judge: a tag a later app adds, or one it refuses.
        for value in ("automatic", *LANGUAGES, "pt-BR", "ja-JP", "it-it", "it"):
            with self.subTest(value):
                self.assertEqual(self._valid(language=value)["language"], value)

    def test_the_language_isnt_empty(self):
        for value in ("", None, ["it-IT"], {"tag": "it-IT"}):
            with self.subTest(repr(value)):
                self._refused(language=value)

    def test_an_unknown_field_is_refused(self):
        self._refused(language="it-IT", temperature="celsius")

    def test_the_wire_values_are_the_sensors_options(self):
        self.assertEqual(managed_devices.LANGUAGE_REGION_AUTOMATIC, "automatic")
        for field, values in REGION_VALUES.items():
            with self.subTest(field):
                options = managed_devices.SENSOR_PROPERTIES[field]["options"]
                self.assertEqual(tuple(options), values)


class CommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = _Hass(asyncio.get_running_loop())
        self.registry = _make_registry(self.hass)
        for device in (FRONT_DESK, MEETING_ROOM, GUEST_ROOM):
            _register(self.registry, device)

    def _ha_device_id(self, device):
        return self.registry.get_device(device[3])["ha_device_id"]

    async def _configure(self, *devices, **fields):
        data = SCHEMA({"device": [self._ha_device_id(d) for d in devices], **fields})
        await managed_devices.handle_managed_device_language_region_configure(
            self.hass, SimpleNamespace(data=data)
        )

    def _sent(self):
        return self.hass.bus.sent

    async def test_only_the_fields_given_in_camel_case(self):
        await self._configure(FRONT_DESK, temperature_unit="fahrenheit", first_weekday="sunday")
        self.assertEqual(
            self._sent(),
            [(
                "device_front_desk",
                "locale.set",
                {"temperatureUnit": "fahrenheit", "firstWeekday": "sunday"},
            )],
        )

    async def test_each_field_on_its_own(self):
        params = {
            "language": ("language", "de-DE"),
            "temperature_unit": ("temperatureUnit", "celsius"),
            "measurement_system": ("measurementSystem", "uk"),
            "clock": ("clock", "12_hour"),
            "first_weekday": ("firstWeekday", "monday"),
        }
        for field, (param, value) in params.items():
            with self.subTest(field):
                self.hass.bus.sent.clear()
                await self._configure(FRONT_DESK, **{field: value})
                self.assertEqual(self._sent()[0][2], {param: value})

    async def test_all_five_and_automatic(self):
        await self._configure(
            FRONT_DESK,
            language="automatic",
            temperature_unit="automatic",
            measurement_system="automatic",
            clock="automatic",
            first_weekday="automatic",
        )
        self.assertEqual(
            self._sent()[0][2],
            {
                "language": "automatic",
                "temperatureUnit": "automatic",
                "measurementSystem": "automatic",
                "clock": "automatic",
                "firstWeekday": "automatic",
            },
        )

    async def test_a_language_the_select_doesnt_list_goes_as_given(self):
        await self._configure(FRONT_DESK, language="pt-BR")
        self.assertEqual(self._sent()[0][2], {"language": "pt-BR"})

    async def test_every_target_whatever_its_state(self):
        # The front desk reports language and region, the meeting room reports
        # nulls, and the guest room is on an app that predates them.
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        _report(self.registry, MEETING_ROOM, {**BASE_STATE, **dict.fromkeys(REPORTED)})
        _report(self.registry, GUEST_ROOM, BASE_STATE)
        await self._configure(FRONT_DESK, MEETING_ROOM, GUEST_ROOM, language="fr-FR")
        self.assertEqual(
            sorted(self._sent()),
            sorted(
                (device[1], "locale.set", {"language": "fr-FR"})
                for device in (FRONT_DESK, MEETING_ROOM, GUEST_ROOM)
            ),
        )

    async def test_an_app_that_predates_it_needs_a_newer_version(self):
        self.hass.bus.answers["device_guest_room"] = _error(-32601, "Method not found")
        with self.assertRaisesRegex(
            managed_devices.BNGntManagedDeviceAppUpdateRequiredError,
            r"^Device 'Guest room' needs a newer version of the Zendo app$",
        ):
            await self._configure(GUEST_ROOM, clock="24_hour")

    async def test_a_value_the_device_refuses_says_why(self):
        message = (
            "language must be automatic or one of: en-GB, en-US, de-DE, es-ES, "
            "fr-FR, it-IT, nl-NL"
        )
        self.hass.bus.answers["device_front_desk"] = _error(-32602, message)
        with self.assertRaises(managed_devices.BNGntManagedDeviceCommandError) as caught:
            await self._configure(FRONT_DESK, language="pt-BR")
        self.assertEqual(str(caught.exception), f"Device returned error: {message}")
        self.assertEqual(caught.exception.code, -32602)

    async def test_an_offline_device_is_sent_nothing(self):
        self.registry.unregister_device(profile_id=GUEST_ROOM[0], device_id=GUEST_ROOM[1])
        with self.assertRaisesRegex(
            managed_devices.BNGntManagedDeviceOfflineError, r"^Device 'Guest room' is offline$"
        ):
            await self._configure(GUEST_ROOM, language="it-IT")
        self.assertEqual(self._sent(), [])


class SensorsTest(unittest.TestCase):
    def setUp(self):
        self.hass = _Hass()
        self.registry = _make_registry(self.hass)
        self.key = _register(self.registry, FRONT_DESK)

    def _added(self):
        return _sensor_keys(self.hass.added_sensors)

    def _sensor(self, prop_key):
        return next(
            entity
            for entity in self.hass.added_sensors
            if entity._attr_unique_id == f"md_{self.key}_{prop_key}"
        )

    def test_none_until_reported(self):
        self.assertTrue(self._added())
        self.assertFalse(self._added() & set(FIELDS))
        self.assertFalse(set(self.registry.get_device(self.key)["optional_entities"]) & set(FIELDS))

    def test_an_app_that_predates_them_gets_none(self):
        _report(self.registry, FRONT_DESK, BASE_STATE)
        self.assertFalse(self._added() & set(FIELDS))

    def test_null_makes_none(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **dict.fromkeys(REPORTED)})
        self.assertFalse(self._added() & set(FIELDS))

    def test_all_five_once_reported(self):
        before = len(self.hass.added_sensors)
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        self.assertEqual(_sensor_keys(self.hass.added_sensors[before:]), set(FIELDS))
        self.assertEqual(
            set(self.registry.get_device(self.key)["optional_entities"]), set(FIELDS)
        )

    def test_each_one_on_its_own(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, "clock": "12_hour"})
        self.assertEqual(self._added() & set(FIELDS), {"clock"})
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        self.assertEqual(self._added() & set(FIELDS), set(FIELDS))

    def test_made_once(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        count = len(self.hass.added_sensors)
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED, "language": "de-DE"})
        self.assertEqual(len(self.hass.added_sensors), count)

    def test_the_reported_values(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        for field, value in REPORTED.items():
            with self.subTest(field):
                self.assertEqual(self._sensor(field).native_value, value)

    def test_the_language_is_a_plain_sensor(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        sensor = self._sensor("language")
        self.assertFalse(hasattr(sensor, "_attr_device_class"))
        self.assertFalse(hasattr(sensor, "_attr_options"))
        # A language a later app adds reads as itself.
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED, "language": "pt-BR"})
        self.assertEqual(sensor.native_value, "pt-BR")

    def test_the_others_are_enums(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        for field, values in REGION_VALUES.items():
            with self.subTest(field):
                sensor = self._sensor(field)
                self.assertIs(sensor._attr_device_class, _SensorDeviceClass.ENUM)
                self.assertEqual(tuple(sensor._attr_options), values)

    def test_an_enum_value_it_doesnt_know_reads_as_unknown(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        _report(
            self.registry,
            FRONT_DESK,
            {
                **BASE_STATE,
                **REPORTED,
                "temperature_unit": "kelvin",
                "measurement_system": "automatic",
                "clock": None,
                "first_weekday": "saturday",
            },
        )
        for field in REGION_VALUES:
            with self.subTest(field):
                self.assertIsNone(self._sensor(field).native_value)

    def test_listed_as_reported(self):
        _report(self.registry, FRONT_DESK, {**BASE_STATE, **REPORTED})
        listed = self.registry.serialize(self.key)["state"]
        self.assertEqual({field: listed[field] for field in FIELDS}, REPORTED)


class StringsTest(unittest.TestCase):
    """Each translated sensor's name and state labels, and the action's fields."""

    def test_every_translated_sensor_has_its_name_and_labels(self):
        sensors = STRINGS["entity"]["sensor"]
        for prop in managed_devices.SENSOR_PROPERTIES.values():
            key = prop.get("translation_key")
            if key is None:
                continue
            with self.subTest(key):
                self.assertTrue(sensors[key]["name"])
                if prop.get("options"):
                    self.assertEqual(set(sensors[key]["state"]), set(prop["options"]))

    def test_the_language_has_no_labels(self):
        self.assertNotIn("state", STRINGS["entity"]["sensor"]["language"])

    def test_the_actions_fields(self):
        action = STRINGS["services"]["managed_device_language_region_configure"]
        self.assertEqual(action["name"], "Configure language and region (Kiosk)")
        self.assertEqual(list(action["fields"]), ["device", *FIELDS])

    def test_the_translation_is_a_copy(self):
        en = json.loads((PACKAGE_DIR / "translations" / "en.json").read_text())
        self.assertEqual(en, STRINGS)


if __name__ == "__main__":
    unittest.main()
