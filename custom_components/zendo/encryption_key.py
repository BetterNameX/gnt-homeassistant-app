"""The site config encryption key: the integration's own copy, in its own store.

The app creates the key in an ``input_text`` helper. A device on the ``zendo/v2/``
commands reads it from here instead, in ``device_api.py``, so the helper can be
removed later. Older apps still read the helper, so it stays for now.

Nothing here writes the helper, and a stored key is never replaced. The rule for
which key is held is ``held_encryption_key`` in ``device_auth.py``. See
docs/managed-devices.md, "The Site Config Encryption Key".

The key never reaches a log line, an error message, an entity, an attribute, a
state or an event.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .device_auth import held_encryption_key, restored_encryption_key

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.site_config_encryption_key"

# The app creates this helper and names it, so keep it in sync with the app.
HELPER_ENTITY_ID = "input_text.bn_gnt_elderflower_config_encryption_key"


class BNGntEncryptionKeyStore:
    """The key this integration holds, in a file only its owner can read."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._key: str | None = None
        self._dirty = False
        self._closed = False

    async def async_load(self) -> None:
        """Load the stored key, or copy the helper's when none is stored.

        ``input_text`` is an after dependency, so its state is there by now.
        """
        self._key = restored_encryption_key(await self._store.async_load())
        self.held()

    async def async_close(self) -> None:
        """Write a pending change now, and take no more.

        Called on unload, so that no write, from a call still under way or at
        Home Assistant's final write, can land after ``async_remove_store`` has
        removed the file, or over the one the next setup loads.
        """
        self._closed = True
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    @property
    def closed(self) -> bool:
        return self._closed

    @callback
    def held(self) -> str | None:
        """Return the key held. A key that comes from the helper is copied to the store."""
        key = held_encryption_key(self._key, self._helper_state)
        if key != self._key:
            self._keep(key)
        return key

    @callback
    def claim(self, key: str) -> str:
        """Keep ``key`` only if no key is held, and return the key kept."""
        held = self.held()
        if held is not None:
            return held
        self._keep(key)
        return key

    def _helper_state(self) -> str | None:
        state = self._hass.states.get(HELPER_ENTITY_ID)
        return state.state if state is not None else None

    @callback
    def _keep(self, key: str) -> None:
        self._key = key
        if self._closed:
            return
        self._dirty = True
        self._store.async_delay_save(self._data_to_save)

    @callback
    def _data_to_save(self) -> dict[str, str]:
        self._dirty = False
        return {"key": self._key}


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the store's file, when the config entry is removed."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()
