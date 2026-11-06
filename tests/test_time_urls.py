"""Tests for custom_components/zendo/time_urls.py.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs nothing from Home Assistant: the module is loaded from its file, not as
part of the integration's package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "zendo" / "time_urls.py"

_spec = importlib.util.spec_from_file_location("bngnt_time_urls", MODULE_PATH)
time_urls = importlib.util.module_from_spec(_spec)
sys.modules["bngnt_time_urls"] = time_urls
_spec.loader.exec_module(time_urls)

PATH = "/api/zendo/time"
PORT = 8123


def build(addresses=(), port=PORT, use_ssl=False, internal_url=None, supervisor_host=None):
    return time_urls.build_time_urls(
        list(addresses), port, use_ssl, internal_url, supervisor_host
    )


def url(host, port=PORT):
    return f"http://{host}:{port}{PATH}"


GARBAGE = [
    None,
    "",
    " ",
    "not a host!",
    0,
    -1,
    1.5,
    True,
    False,
    b"192.168.1.20",
    bytearray(b"x"),
    [],
    [["192.168.1.20"]],
    {},
    {"address": "192.168.1.20"},
    object(),
    IPv6Address("fe80::1"),
    float("nan"),
    "\x00",
    "é" * 300,
    "x" * 100_000,
]


class BuildTimeUrlsTest(unittest.TestCase):
    def test_no_addresses_and_no_host_name(self):
        self.assertEqual(build(), [])

    def test_one_url_per_private_address(self):
        self.assertEqual(
            build(["192.168.1.20", "10.0.0.5", "172.16.0.1", "169.254.10.20"]),
            [
                url("192.168.1.20"),
                url("10.0.0.5"),
                url("172.16.0.1"),
                url("169.254.10.20"),
            ],
        )

    def test_private_range_edges(self):
        self.assertEqual(
            build(["10.255.255.255", "172.31.255.255", "192.168.255.255"]),
            [url("10.255.255.255"), url("172.31.255.255"), url("192.168.255.255")],
        )

    def test_public_and_other_addresses_are_left_out(self):
        self.assertEqual(
            build(
                [
                    "8.8.8.8",
                    "172.15.255.255",
                    "172.32.0.1",
                    "192.169.0.1",
                    "11.0.0.1",
                    "100.64.0.1",
                    "127.0.0.1",
                    "0.0.0.0",
                    "255.255.255.255",
                    "224.0.0.251",
                    "192.168.1.20",
                ]
            ),
            [url("192.168.1.20")],
        )

    def test_ipv6_is_ignored(self):
        self.assertEqual(
            build(
                [
                    "fe80::1",
                    "fe80::1%2",
                    "fd00::1",
                    "::1",
                    "::ffff:192.168.1.20",
                    IPv6Address("fd00::2"),
                ]
            ),
            [],
        )

    def test_address_objects_are_taken(self):
        self.assertEqual(build([IPv4Address("192.168.1.20")]), [url("192.168.1.20")])

    def test_addresses_that_dont_parse_are_skipped(self):
        self.assertEqual(
            build(
                [
                    "192.168.1",
                    "192.168.1.256",
                    "192.168.001.020",
                    " 192.168.1.20",
                    "192.168.1.20 ",
                    "192.168.1.20/24",
                    "192.168.1.20:8123",
                    "http://192.168.1.21",
                    "10.0.0.5",
                ]
            ),
            [url("10.0.0.5")],
        )

    def test_ssl_gives_nothing(self):
        self.assertEqual(
            build(
                ["192.168.1.20"],
                use_ssl=True,
                internal_url="https://ha.local",
                supervisor_host="homeassistant",
            ),
            [],
        )

    def test_every_url_is_plain_http(self):
        urls = build(
            ["192.168.1.20", "10.0.0.5"],
            internal_url="https://ha.local:8443",
            supervisor_host="homeassistant",
        )
        self.assertEqual(len(urls), 3)
        for each in urls:
            self.assertTrue(each.startswith("http://"), each)

    def test_the_port_is_home_assistants(self):
        self.assertEqual(
            build(["192.168.1.20"], port=443, internal_url="http://ha.local:9999/x"),
            [url("192.168.1.20", port=443), url("ha.local", port=443)],
        )

    def test_no_port_or_no_ssl_flag_gives_nothing(self):
        for port in [None, 0, -1, 65536, "8123", 8123.0, True, False]:
            with self.subTest(port=port):
                self.assertEqual(
                    build(["192.168.1.20"], port=port, supervisor_host="homeassistant"),
                    [],
                )
        for use_ssl in [True, None, 0, 1, "true", "false", "", []]:
            with self.subTest(use_ssl=use_ssl):
                self.assertEqual(
                    build(["192.168.1.20"], use_ssl=use_ssl, supervisor_host="homeassistant"),
                    [],
                )

    def test_port_range_edges(self):
        self.assertEqual(build(["10.0.0.5"], port=1), [url("10.0.0.5", port=1)])
        self.assertEqual(build(["10.0.0.5"], port=65535), [url("10.0.0.5", port=65535)])

    def test_local_host_name_from_the_internal_url(self):
        self.assertEqual(
            build(["192.168.1.20"], internal_url="http://homeassistant.local:8123"),
            [url("192.168.1.20"), url("homeassistant.local")],
        )

    def test_internal_url_host_name_is_lowercased(self):
        self.assertEqual(
            build(internal_url="http://Front-Desk.Office.LOCAL:8123"),
            [url("front-desk.office.local")],
        )

    def test_internal_url_with_user_info_gives_its_host_alone(self):
        self.assertEqual(
            build(internal_url="http://user:secret@ha.local:8123/path?x=1#y"),
            [url("ha.local")],
        )

    def test_local_host_name_from_the_supervisor(self):
        self.assertEqual(
            build(["192.168.1.20"], supervisor_host="homeassistant"),
            [url("192.168.1.20"), url("homeassistant.local")],
        )

    def test_supervisor_host_name_is_lowercased(self):
        self.assertEqual(build(supervisor_host="HomeAssistant"), [url("homeassistant.local")])

    def test_internal_url_comes_before_the_supervisor(self):
        self.assertEqual(
            build(internal_url="http://office-hub.local:8123", supervisor_host="homeassistant"),
            [url("office-hub.local")],
        )

    def test_internal_url_that_isnt_local_falls_back_to_the_supervisor(self):
        for internal_url in [
            "http://192.168.1.20:8123",
            "https://ha.example.com",
            "http://localhost:8123",
            "http://local",
            "http://.local",
            "http://ha.localhost",
            "http://ha.local.example.com",
            "http://ha..local",
            "http://-ha.local",
            "http://ha-.local",
            "http://ha_hub.local",
            "http://[fe80::1]:8123",
            "http://[::1",
            "not a url",
            "",
        ]:
            with self.subTest(internal_url=internal_url):
                self.assertEqual(
                    build(internal_url=internal_url, supervisor_host="homeassistant"),
                    [url("homeassistant.local")],
                )

    def test_non_local_host_names_are_ignored(self):
        for internal_url in [
            "https://ha.example.com",
            "https://abcdef0123456789.ui.nabu.casa",
            "http://homeassistant.lan:8123",
            "http://homeassistant:8123",
            "http://192.168.1.20:8123",
        ]:
            with self.subTest(internal_url=internal_url):
                self.assertEqual(build(internal_url=internal_url), [])

    def test_supervisor_host_name_must_be_one_label(self):
        for host in [
            "",
            "home assistant",
            "home.assistant",
            "homeassistant.local",
            "-ha",
            "ha-",
            "ha_hub",
            "a" * 64,
            "hä",
        ]:
            with self.subTest(host=host):
                self.assertEqual(build(supervisor_host=host), [])

    def test_at_most_four(self):
        addresses = [f"192.168.1.{n}" for n in range(1, 8)]
        self.assertEqual(
            build(addresses, internal_url="http://ha.local:8123"),
            [url(address) for address in addresses[:4]],
        )

    def test_the_host_name_fills_a_free_place(self):
        addresses = ["192.168.1.1", "192.168.1.2", "192.168.1.3"]
        self.assertEqual(
            build(addresses, supervisor_host="homeassistant"),
            [*(url(address) for address in addresses), url("homeassistant.local")],
        )

    def test_duplicates_are_dropped(self):
        self.assertEqual(
            build(
                [
                    "192.168.1.20",
                    "192.168.1.20",
                    IPv4Address("192.168.1.20"),
                    "10.0.0.5",
                    "192.168.1.20",
                ]
            ),
            [url("192.168.1.20"), url("10.0.0.5")],
        )

    def test_duplicates_dont_use_up_places(self):
        addresses = ["192.168.1.20"] * 10 + ["10.0.0.5"]
        self.assertEqual(
            build(addresses, supervisor_host="homeassistant"),
            [url("192.168.1.20"), url("10.0.0.5"), url("homeassistant.local")],
        )

    def test_urls_longer_than_256_characters_are_left_out(self):
        long_host = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 37]) + ".local"
        self.assertLessEqual(len(long_host), 253)
        self.assertGreater(len(url(long_host, port=65535)), 256)
        self.assertEqual(
            build(["192.168.1.20"], port=65535, internal_url=f"http://{long_host}"),
            [url("192.168.1.20", port=65535)],
        )

    def test_every_url_fits(self):
        host = ".".join(["a" * 63, "b" * 63, "c" * 63]) + ".local"
        urls = build(["192.168.1.20"], port=65535, internal_url=f"http://{host}")
        self.assertEqual(len(urls), 2)
        for each in urls:
            self.assertLessEqual(len(each), 256)

    def test_odd_inputs_never_raise(self):
        for value in GARBAGE:
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(
                    time_urls.build_time_urls(value, value, value, value, value), []
                )
                self.assertEqual(build([value]), [])
                self.assertEqual(build(internal_url=value), [])
                self.assertEqual(build(supervisor_host=value), [])
                self.assertEqual(
                    time_urls.build_time_urls(value, PORT, False, None, None), []
                )

    def test_addresses_that_arent_a_list_are_ignored(self):
        for addresses in ["192.168.1.20", b"192.168.1.20", {"192.168.1.20"}, None, 3]:
            with self.subTest(addresses=addresses):
                self.assertEqual(
                    time_urls.build_time_urls(addresses, PORT, False, None, "homeassistant"),
                    [url("homeassistant.local")],
                )

    def test_a_tuple_of_addresses_is_taken(self):
        self.assertEqual(
            time_urls.build_time_urls(("192.168.1.20",), PORT, False, None, None),
            [url("192.168.1.20")],
        )

    def test_every_url_is_a_string(self):
        urls = build(
            ["192.168.1.20", IPv4Address("10.0.0.5")], supervisor_host="homeassistant"
        )
        self.assertEqual(len(urls), 3)
        for each in urls:
            self.assertIs(type(each), str)


def adapter(name, addresses, enabled=True, default=False, ipv6=()):
    return {
        "name": name,
        "index": 1,
        "enabled": enabled,
        "auto": enabled,
        "default": default,
        "ipv4": [{"address": address, "network_prefix": 24} for address in addresses],
        "ipv6": [
            {"address": address, "flowinfo": 0, "scope_id": 2, "network_prefix": 64}
            for address in ipv6
        ],
    }


class EnabledIpv4AddressesTest(unittest.TestCase):
    def test_enabled_adapters_only(self):
        self.assertEqual(
            time_urls.enabled_ipv4_addresses(
                [
                    adapter("eth0", ["192.168.1.20"]),
                    adapter("docker0", ["172.17.0.1"], enabled=False),
                    adapter("wlan0", ["10.0.0.5", "10.0.0.6"]),
                ]
            ),
            ["192.168.1.20", "10.0.0.5", "10.0.0.6"],
        )

    def test_default_adapter_first(self):
        self.assertEqual(
            time_urls.enabled_ipv4_addresses(
                [
                    adapter("hassio", ["172.30.32.1"]),
                    adapter("eth0", ["192.168.1.20"], default=True),
                    adapter("wlan0", ["10.0.0.5"]),
                ]
            ),
            ["192.168.1.20", "172.30.32.1", "10.0.0.5"],
        )

    def test_ipv6_is_ignored(self):
        self.assertEqual(
            time_urls.enabled_ipv4_addresses(
                [adapter("eth0", [], ipv6=["fe80::1", "fd00::1"], default=True)]
            ),
            [],
        )

    def test_enabled_must_be_true(self):
        for enabled in [None, 1, "true", "yes"]:
            with self.subTest(enabled=enabled):
                entry = adapter("eth0", ["192.168.1.20"])
                entry["enabled"] = enabled
                self.assertEqual(time_urls.enabled_ipv4_addresses([entry]), [])

    def test_no_adapters(self):
        self.assertEqual(time_urls.enabled_ipv4_addresses([]), [])

    def test_odd_inputs_never_raise(self):
        for adapters in [
            *GARBAGE,
            [None, "eth0", 3, [], object()],
            [{"enabled": True}],
            [{"enabled": True, "ipv4": None}],
            [{"enabled": True, "ipv4": "192.168.1.20"}],
            [{"enabled": True, "ipv4": [None, "192.168.1.20", 3, {}, {"address": 3}]}],
            [{"enabled": True, "ipv4": [{"address": None}]}],
        ]:
            with self.subTest(adapters=repr(adapters)[:60]):
                self.assertEqual(time_urls.enabled_ipv4_addresses(adapters), [])

    def test_good_entries_beside_bad_ones_are_kept(self):
        self.assertEqual(
            time_urls.enabled_ipv4_addresses(
                [
                    None,
                    {"enabled": True, "ipv4": [None, {"address": 3}, {"address": "10.0.0.5"}]},
                    adapter("eth0", ["192.168.1.20"]),
                ]
            ),
            ["10.0.0.5", "192.168.1.20"],
        )

    def test_feeds_the_builder(self):
        self.assertEqual(
            build(
                time_urls.enabled_ipv4_addresses(
                    [
                        adapter("lo", ["127.0.0.1"]),
                        adapter("eth0", ["192.168.1.20"], default=True, ipv6=["fe80::1"]),
                        adapter("tailscale0", ["100.101.102.103"]),
                    ]
                ),
                supervisor_host="homeassistant",
            ),
            [url("192.168.1.20"), url("homeassistant.local")],
        )


class _HostInfo:
    def __init__(self, hostname):
        self.hostname = hostname


class _Raises:
    @property
    def hostname(self):
        raise RuntimeError("not ready")


class SupervisorHostnameTest(unittest.TestCase):
    def test_from_a_dict(self):
        self.assertEqual(
            time_urls.supervisor_hostname({"hostname": "homeassistant", "chassis": "embedded"}),
            "homeassistant",
        )

    def test_from_an_object(self):
        self.assertEqual(time_urls.supervisor_hostname(_HostInfo("homeassistant")), "homeassistant")

    def test_missing_or_not_a_string(self):
        for host_info in [
            None,
            {},
            {"hostname": None},
            {"hostname": 3},
            _HostInfo(None),
            _HostInfo(["homeassistant"]),
            _Raises(),
            *GARBAGE,
        ]:
            with self.subTest(host_info=repr(host_info)[:40]):
                self.assertIsNone(time_urls.supervisor_hostname(host_info))


class TimeAnswerBodyTest(unittest.TestCase):
    def test_now_in_epoch_milliseconds(self):
        body = time_urls.time_answer_body(1793232000123)
        self.assertIsInstance(body, bytes)
        self.assertEqual(json.loads(body), {"now": 1793232000123})
        self.assertIs(type(json.loads(body)["now"]), int)


if __name__ == "__main__":
    unittest.main()
