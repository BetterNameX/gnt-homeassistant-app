"""The signing keys of the device control backend token, read from DNS.

The keys are one TXT record, ``device-control-backend.v1.keys.gnt.betterna.me``,
read over DNS over HTTPS and taken only from a DNSSEC-validated answer. The last
record read is kept in its own store, so tokens can still be checked after a
restart while Zendo's cloud or DNS can't be reached. See docs/managed-devices.md,
"Signing Keys".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .device_auth import KeysRecord, SigningKey, parse_keys_record
from .dns_doh import resolve_txt_validated

_LOGGER = logging.getLogger(__name__)

# Unknown signing algorithms already logged, so each is logged once.
_logged_unknown_algorithms: set[str] = set()

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.device_control_backend_keys"

KEYS_HOSTNAME = "device-control-backend.v1.keys"

# Read again after this long when the record has no ttl of its own.
DEFAULT_TTL = 3600
# The least time between two reads, whatever asks for one.
MIN_READ_INTERVAL = 60


class BNGntSigningKeys:
    """The signing keys record last read from DNS, kept across restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._record: KeysRecord | None = None
        # Unix seconds of the last validated read.
        self._read_at: float | None = None
        # Monotonic seconds of the last attempt, validated or not.
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load the record kept from the last read."""
        data = await self._store.async_load()
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            return
        try:
            self._record = parse_keys_record(data["content"])
        except ValueError:
            _LOGGER.warning("Ignoring the stored signing keys, which can't be read")
            return
        read_at = data.get("read_at")
        self._read_at = read_at if isinstance(read_at, (int, float)) else None

    def keys(self) -> tuple[SigningKey, ...]:
        return self._record.keys if self._record else ()

    def _is_stale(self) -> bool:
        if self._record is None or self._read_at is None:
            return True
        ttl = self._record.ttl or DEFAULT_TTL
        return time.time() - self._read_at >= ttl

    def _may_read(self) -> bool:
        return (
            self._attempted_at is None
            or time.monotonic() - self._attempted_at >= MIN_READ_INTERVAL
        )

    async def async_keys_for(self, kid: str | None) -> tuple[SigningKey, ...]:
        """Return the keys to check a token naming ``kid`` with.

        Waits for a read only when no key kept is ``kid``. When the record is
        only due, it's read again in the background, and the keys kept are used
        meanwhile, each until its ``exp``, as they are when it can't be read.
        """
        if kid is not None and kid not in {key.kid for key in self.keys()}:
            await self._async_read()
        elif self._is_stale() and self._may_read():
            self._entry.async_create_background_task(
                self._hass, self._async_read(), "bngnt_read_signing_keys"
            )
        return self.keys()

    async def _async_read(self) -> None:
        async with self._lock:
            if not self._may_read():
                return
            self._attempted_at = time.monotonic()

            content = await resolve_txt_validated(
                async_get_clientsession(self._hass), KEYS_HOSTNAME
            )
            if content is None:
                _LOGGER.warning(
                    "Couldn't read the signing keys; using the ones kept from before"
                )
                return

            try:
                record = parse_keys_record(content)
            except ValueError:
                _LOGGER.warning("Ignoring a signing keys record that can't be read")
                return
            if record.skipped:
                _LOGGER.warning(
                    "Left out %s signing keys that can't be used", record.skipped
                )
            new_algorithms = record.unknown_algorithms - _logged_unknown_algorithms
            if new_algorithms:
                _logged_unknown_algorithms.update(new_algorithms)
                _LOGGER.info(
                    "Ignoring signing keys for algorithms this version doesn't support: %s",
                    ", ".join(sorted(new_algorithms)),
                )

            self._record = record
            self._read_at = time.time()
            await self._store.async_save({"content": content, "read_at": self._read_at})


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()
