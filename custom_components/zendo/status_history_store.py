"""The status history's store. See ``status_history.py`` and
docs/managed-devices.md, "Status History"."""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .status_history import SAVE_DELAY, BNGntStatusHistory

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.managed_device_status_history"


class BNGntStatusHistoryStore:
    """Keeps ``history`` in its own store, never in the config entry's data.

    It writes ``SAVE_DELAY`` after the first change since its last write, on
    its own timer: see docs/managed-devices.md, "History Storage".
    """

    def __init__(self, hass: HomeAssistant, restart_window: float) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self.history = BNGntStatusHistory(
            dt_util.get_default_time_zone, restart_window, self._schedule_save
        )
        self._cancel_save: CALLBACK_TYPE | None = None
        self._final_write_unsub: CALLBACK_TYPE | None = None
        self._closed = False

    async def async_load(self, managed_device_ids: Iterable[str]) -> None:
        """Load the history, dropping that of any record that's gone."""
        self._final_write_unsub = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_FINAL_WRITE, self._async_final_write
        )
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
        if self._final_write_unsub is not None:
            self._final_write_unsub()
            self._final_write_unsub = None
        await self._async_save_now()

    @callback
    def _schedule_save(self) -> None:
        if self._closed or self._cancel_save is not None:
            return
        self._cancel_save = async_call_later(self._hass, SAVE_DELAY, self._save_due)

    @callback
    def _save_due(self, _now: datetime) -> None:
        self._cancel_save = None
        # No delay, so nothing Store does with a later call can put it off.
        self._store.async_delay_save(self.history.data_to_save)

    async def _async_final_write(self, _event: Event) -> None:
        # Removing a once-listener after it has fired logs an error.
        self._final_write_unsub = None
        await self._async_save_now()

    async def _async_save_now(self) -> None:
        if self._cancel_save is not None:
            self._cancel_save()
            self._cancel_save = None
        if self.history.has_unsaved_changes:
            await self._store.async_save(self.history.data_to_save())


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()
