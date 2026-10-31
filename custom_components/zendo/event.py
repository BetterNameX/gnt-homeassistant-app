"""Event platform.

Creates an EventEntity per device group, the wall panels and tablets that
belong to the property rather than to a person. A device group can't receive push
notifications, so it gets no notify entity: this entity is what
``zendo.ring_doorbell`` targets instead, and it records each ring sent to it.
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CACHED_PROFILES, DOMAIN, SERVICE_DEVICE_IDENTIFIER
from .profiles import device_groups

EVENT_TYPE_RING = "ring"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device group entities from a config entry."""
    # Store the callback so __init__.py can add entities after a profile refresh
    hass.data[DOMAIN]["async_add_device_group_entities"] = async_add_entities

    # Create entities from cached profiles (no API call at boot)
    cached_device_groups = device_groups(entry.data.get(CONF_CACHED_PROFILES, []))

    if cached_device_groups:
        async_add_entities(
            [
                BNGntDeviceGroupEventEntity(entry, p["id"], p["label"])
                for p in cached_device_groups
            ]
        )


class BNGntDeviceGroupEventEntity(EventEntity):
    """A doorbell ring target representing a single device group."""

    _attr_has_entity_name = True
    _attr_translation_key = "device_group"

    def __init__(
        self,
        entry: ConfigEntry,
        profile_id: str,
        label: str,
    ) -> None:
        """Initialise the device group entity."""
        self._entry = entry
        self._profile_id = profile_id
        self._attr_name = label
        self._attr_unique_id = profile_id
        self._attr_event_types = [EVENT_TYPE_RING]

    @property
    def profile_id(self) -> str:
        """Return the device group's profile ID."""
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

    @callback
    def async_record_ring(self, deep_link_destination: str) -> None:
        """Record a doorbell ring sent to this device group."""
        self._trigger_event(
            EVENT_TYPE_RING, {"deep_link_destination": deep_link_destination}
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register this entity in hass.data so the services can resolve it."""
        entities: dict[str, BNGntDeviceGroupEventEntity] = self.hass.data[
            DOMAIN
        ].setdefault("device_group_entities", {})
        entities[self.entity_id] = self

    async def async_will_remove_from_hass(self) -> None:
        """Deregister this entity from hass.data."""
        entities: dict[str, BNGntDeviceGroupEventEntity] = self.hass.data[
            DOMAIN
        ].get("device_group_entities", {})
        entities.pop(self.entity_id, None)

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Re-key this entity in hass.data so the services still resolve it."""
        super().async_entity_id_changed(old_entity_id)
        entities: dict[str, BNGntDeviceGroupEventEntity] = self.hass.data[
            DOMAIN
        ].setdefault("device_group_entities", {})
        entities.pop(old_entity_id, None)
        entities[self.entity_id] = self
