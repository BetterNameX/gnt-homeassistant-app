"""DNS over HTTPS (DoH) query utility with TTL-based caching.

Port of the app's dns-txt-kv.ts - resolves DNS TXT records via DoH
(Cloudflare, then Google) and caches results respecting the TTL embedded
in the record's JSON payload.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .device_auth import parse_txt_data

_LOGGER = logging.getLogger(__name__)

DNS_BASE_DOMAIN = "gnt.betterna.me"

TXT_RECORD_TYPE = 16

DOH_ENDPOINTS = [
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
]

REQUEST_TIMEOUT = ClientTimeout(total=5)


class BNGntDnsReadError(RuntimeError):
    """A TXT record can't be read, and no earlier answer is cached."""


@dataclass
class _CacheEntry:
    values: dict[str, Any]
    expiry_time: float | None


_cache: dict[str, _CacheEntry] = {}


async def query_dns_txt(
    session: ClientSession,
    hostname: str,
    cache_key: str,
) -> dict[str, Any]:
    """Query a DNS TXT record via DoH with TTL-based caching.

    Mirrors the behaviour of the app's queryDnsWithCache:
    1. Return cached data if still within TTL.
    2. Try to resolve via DoH (Cloudflare, then Google).
    3. Fall back to stale cached data on failure.
    4. Raise if no data is available at all.
    """
    now = time.monotonic()
    stale_data: dict[str, Any] | None = None

    cached = _cache.get(cache_key)

    if cached is not None:
        if cached.expiry_time is not None and cached.expiry_time >= now:
            _LOGGER.debug(
                "[DNS %s] Using cached values (expires in %.0fs)",
                hostname,
                cached.expiry_time - now,
            )
            return cached.values

        stale_data = cached.values

    # Try DoH endpoints in order
    fqdn = f"{hostname}.{DNS_BASE_DOMAIN}"

    for endpoint in DOH_ENDPOINTS:
        try:
            data = await _resolve_txt(session, endpoint, fqdn)

            if data is not None:
                ttl = data.get("ttl")
                expiry = (
                    now + ttl
                    if isinstance(ttl, (int, float)) and ttl > 0
                    else None
                )

                _cache[cache_key] = _CacheEntry(values=data, expiry_time=expiry)
                _LOGGER.debug("[DNS %s] Resolved via %s: %s", hostname, endpoint, data)
                return data

        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "[DNS %s] Failed to resolve via %s", hostname, endpoint, exc_info=True
            )

    if stale_data is not None:
        _LOGGER.debug("[DNS %s] Using stale cached data", hostname)
        return stale_data

    raise BNGntDnsReadError(f"Cannot fetch DNS data for {hostname}")


async def resolve_txt_validated(session: ClientSession, hostname: str) -> str | None:
    """Return a TXT record's text from the first DNSSEC-validated answer.

    Tries each DoH endpoint in turn, and takes an answer only when the resolver
    validated it (``AD`` is true). An answer that can't be read counts as a
    failed read, and the next endpoint is tried. Returns None when none did.
    Nothing is cached here: the caller keeps what it needs.
    """
    fqdn = f"{hostname}.{DNS_BASE_DOMAIN}"

    for endpoint in DOH_ENDPOINTS:
        try:
            result = await _query_txt(session, endpoint, fqdn)
            if result.get("AD") is not True:
                _LOGGER.warning(
                    "[DNS %s] %s didn't return a validated answer", hostname, endpoint
                )
                continue
            text = _first_txt(result)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "[DNS %s] Failed to resolve via %s", hostname, endpoint, exc_info=True
            )
            continue

        if text is not None:
            return text

    return None


async def _query_txt(
    session: ClientSession,
    endpoint: str,
    fqdn: str,
) -> dict[str, Any]:
    """Return a DoH JSON endpoint's answer for a TXT query."""
    params = {"name": fqdn, "type": "TXT"}
    headers = {"Accept": "application/dns-json"}

    async with session.get(
        endpoint, params=params, headers=headers, timeout=REQUEST_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


def _first_txt(result: dict[str, Any]) -> str | None:
    """Return the text of the first TXT answer, its strings joined."""
    for answer in result.get("Answer", []):
        if answer.get("type", TXT_RECORD_TYPE) != TXT_RECORD_TYPE:
            continue
        text = parse_txt_data(answer.get("data", ""))
        if text:
            return text
    return None


async def _resolve_txt(
    session: ClientSession,
    endpoint: str,
    fqdn: str,
) -> dict[str, Any] | None:
    """Resolve a single TXT record via a DoH JSON endpoint."""
    text = _first_txt(await _query_txt(session, endpoint, fqdn))
    return json.loads(text) if text is not None else None
