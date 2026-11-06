"""The time view's answer, and the URLs that reach it.

Plain logic with no Home Assistant import, so the tests can load this module on
its own. ``time_view.py`` wires it into Home Assistant. Every function here
takes any value and never raises: one it can't use gives nothing. See
docs/managed-devices.md, "The Time View".
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Network
from typing import Any
from urllib.parse import urlsplit

TIME_VIEW_PATH = "/api/zendo/time"

MAX_TIME_URLS = 4
MAX_TIME_URL_LENGTH = 256

_PRIVATE_IPV4_NETWORKS = tuple(
    IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)

_MAX_HOSTNAME_LENGTH = 253
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_LABEL = re.compile(_LABEL)
_LOCAL_HOSTNAME = re.compile(rf"(?:{_LABEL}\.)+local")


def time_answer_body(now_ms: int) -> bytes:
    """The view's JSON answer, ``{"now": <epoch milliseconds>}``."""
    return json.dumps({"now": now_ms}).encode()


def enabled_ipv4_addresses(adapters: Any) -> list[str]:
    """The IPv4 addresses of the enabled network adapters, the default adapter's first.

    ``adapters`` is the network integration's list of adapters, each a dict
    with ``enabled``, ``default`` and ``ipv4``, a list of ``{"address": ...}``.
    """
    if not isinstance(adapters, (list, tuple)):
        return []

    default: list[str] = []
    other: list[str] = []
    for adapter in adapters:
        if not isinstance(adapter, Mapping) or adapter.get("enabled") is not True:
            continue
        ipv4 = adapter.get("ipv4")
        if not isinstance(ipv4, (list, tuple)):
            continue
        addresses = default if adapter.get("default") is True else other
        for entry in ipv4:
            if isinstance(entry, Mapping) and isinstance(entry.get("address"), str):
                addresses.append(entry["address"])
    return default + other


def supervisor_hostname(host_info: Any) -> str | None:
    """The host name in the Supervisor's host info: a dict, or an object with ``hostname``."""
    try:
        if isinstance(host_info, Mapping):
            hostname = host_info.get("hostname")
        else:
            hostname = getattr(host_info, "hostname", None)
    except Exception:  # noqa: BLE001
        return None
    return hostname if isinstance(hostname, str) else None


def build_time_urls(
    addresses: Any,
    port: Any,
    use_ssl: Any,
    internal_url: Any,
    supervisor_host: Any,
) -> list[str]:
    """The time view's ``http`` URLs: one per private IPv4 address, then a ``.local`` host name.

    - ``addresses``: a list of IPv4 addresses, as strings or ``IPv4Address``.
      Only those in 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and
      169.254.0.0/16 are used, so IPv6, loopback and public addresses aren't.
    - ``port``: Home Assistant's HTTP port. ``use_ssl``: whether that port
      serves ``https``, in which case it serves no plain HTTP. Unless the port
      is valid and ``use_ssl`` is ``False``, there are no URLs.
    - The host name is ``internal_url``'s host when it's a ``.local`` name,
      otherwise ``supervisor_host`` plus ``.local`` when it's a single label.
      No other host name is used.

    At most ``MAX_TIME_URLS``, in that order, with no duplicates and none
    longer than ``MAX_TIME_URL_LENGTH``.
    """
    if not _is_port(port) or use_ssl is not False:
        return []

    hosts: list[str] = []
    if isinstance(addresses, (list, tuple)):
        hosts.extend(
            address for address in map(_private_ipv4, addresses) if address is not None
        )
    hostname = _local_hostname_from_url(internal_url) or _local_hostname_from_label(
        supervisor_host
    )
    if hostname is not None:
        hosts.append(hostname)

    urls: list[str] = []
    for host in hosts:
        url = f"http://{host}:{port}{TIME_VIEW_PATH}"
        if len(url) > MAX_TIME_URL_LENGTH or url in urls:
            continue
        urls.append(url)
        if len(urls) == MAX_TIME_URLS:
            break
    return urls


def _is_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _private_ipv4(value: Any) -> str | None:
    if isinstance(value, IPv4Address):
        address = value
    elif isinstance(value, str):
        try:
            address = IPv4Address(value)
        except ValueError:
            return None
    else:
        return None
    if any(address in network for network in _PRIVATE_IPV4_NETWORKS):
        return str(address)
    return None


def _local_hostname_from_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except Exception:  # noqa: BLE001
        return None
    if (
        isinstance(host, str)
        and len(host) <= _MAX_HOSTNAME_LENGTH
        and _LOCAL_HOSTNAME.fullmatch(host)
    ):
        return host
    return None


def _local_hostname_from_label(label: Any) -> str | None:
    if not isinstance(label, str):
        return None
    label = label.lower()
    if _HOST_LABEL.fullmatch(label):
        return f"{label}.local"
    return None
