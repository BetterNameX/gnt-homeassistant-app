"""Device registrations: each device's API key hash and encryption public key,
and the tombstones of revoked devices, kept in their own store.

The book itself, ``RegistrationBook``, is in ``device_auth.py``. See
docs/managed-devices.md, "Device Registration (v2)".

An API key never reaches a log line, an error message, an entity, an attribute,
a state or an event: only the registration's ids do.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .device_auth import RegistrationBook

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.device_registrations"


class BNGntRegistrationStore:
    """The registration book, in a file only its owner can read."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self.book = RegistrationBook()
        self._dirty = False
        self._closed = False

    async def async_load(self) -> None:
        self.book = RegistrationBook.from_data(await self._store.async_load())

    async def async_close(self) -> None:
        """Write a pending change now, and take no more.

        Called on unload, so that no write, from a call still under way or at
        Home Assistant's final write, can land after ``async_remove_store`` has
        removed the file, or over the one the next setup loads.
        """
        self._closed = True
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    @callback
    def schedule_save(self) -> None:
        if self._closed:
            return
        self._dirty = True
        self._store.async_delay_save(self._data_to_save)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        self._dirty = False
        return self.book.to_data()


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()
