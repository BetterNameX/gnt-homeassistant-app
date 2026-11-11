"""The status history's store. See ``status_history.py`` and
docs/managed-devices.md, "Status History"."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .status_history import BNGntStatusHistory

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.managed_device_status_history"

# Seconds. Losing the last minute on a crash is acceptable.
SAVE_DELAY = 60


class BNGntStatusHistoryStore:
    """Keeps ``history`` in its own store, never in the config entry's data."""

    def __init__(self, hass: HomeAssistant, restart_window: float) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self.history = BNGntStatusHistory(
            dt_util.get_default_time_zone, restart_window, self._schedule_save
        )
        self._dirty = False
        self._closed = False

    async def async_load(self, managed_device_ids: Iterable[str]) -> None:
        """Load the history, dropping that of any record that's gone."""
        self.history.load(
            await self._store.async_load(), managed_device_ids, time.time()
        )

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
    def _schedule_save(self) -> None:
        if self._closed:
            return
        self._dirty = True
        # The store keeps the earliest pending write, so changes in a busy
        # minute don't push it back.
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        self._dirty = False
        return self.history.as_data()


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()
