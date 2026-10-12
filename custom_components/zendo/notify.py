"""Notify platform.

Creates a NotifyEntity per person so that users can target them via both
the built-in ``notify.send_message`` and the custom
``zendo.send_notification`` service. Device groups can't receive push
notifications, so they get an event entity instead (see event.py).
"""

from __future__ import annotations

import logging

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import send_notification
from .const import (
    CONF_CACHED_PROFILES,
    CONF_PUSH_NOTIFICATION_TOKEN,
    DOMAIN,
    SERVICE_DEVICE_IDENTIFIER,
)
from .profiles import people

_LOGGER = logging.getLogger(__name__)


def _build_notification(profile_id: str, message: str) -> dict:
    """Build a GraphQL notification input dict (no interruption level)."""
    return {
        "profileId": profile_id,
        "body": {"en": message.strip()},
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up notify entities from a config entry."""
    # Store the callback so __init__.py can add entities after a profile refresh
    hass.data[DOMAIN]["async_add_notify_entities"] = async_add_entities

    # Create entities from cached profiles (no API call at boot)
    cached_people = people(entry.data.get(CONF_CACHED_PROFILES, []))

    if cached_people:
        async_add_entities(
            [BNGntNotifyEntity(entry, p["id"], p["label"]) for p in cached_people],
            update_before_add=True,
        )


class BNGntNotifyEntity(NotifyEntity):
    """A notification target representing a single person."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        profile_id: str,
        label: str,
    ) -> None:
        """Initialise the notify entity."""
        self._entry = entry
        self._profile_id = profile_id
        self._attr_name = label
        self._attr_unique_id = profile_id

    @property
    def profile_id(self) -> str:
        """Return the person's profile ID."""
        return self._profile_id

    @property
    def device_info(self) -> DeviceInfo:
        """Group under the shared device."""
        return DeviceInfo(
            identifiers={(DOMAIN, SERVICE_DEVICE_IDENTIFIER)},
            name="Zendo",
            manufacturer="Zendo",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send a basic push notification (no interruption level)."""
        token = self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)

        if not isinstance(token, str) or not token:
            raise HomeAssistantError(
                "Push notifications have not been enabled. "
                "Please open the Zendo iOS/Android app to enable push notifications."
            )

        notification = _build_notification(self._profile_id, message)
        await send_notification(self.hass, token, [notification])

    async def async_added_to_hass(self) -> None:
        """Register this entity in hass.data so the services can resolve it."""
        entities: dict[str, BNGntNotifyEntity] = self.hass.data[DOMAIN].setdefault(
            "notify_entities", {}
        )
        entities[self.entity_id] = self

    async def async_will_remove_from_hass(self) -> None:
        """Deregister this entity from hass.data."""
        entities: dict[str, BNGntNotifyEntity] = self.hass.data[DOMAIN].get(
            "notify_entities", {}
        )
        entities.pop(self.entity_id, None)
