"""Binary sensor platform."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_PUSH_NOTIFICATION_TOKEN,
    DOMAIN,
    INTEGRATION_VERSION,
    SERVICE_DEVICE_IDENTIFIER,
    SIGNAL_CONFIG_UPDATED,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from a config entry."""
    hass.data[DOMAIN]["async_add_managed_device_binary_sensors"] = async_add_entities
    async_add_entities([BNGntStatusBinarySensor(entry)])


class BNGntStatusBinarySensor(BinarySensorEntity):
    """Binary sensor indicating whether the API is configured and available."""

    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False
    _attr_unique_id = "status"

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise the sensor."""
        self._entry = entry

    @property
    def is_on(self) -> bool:
        """Return True - the API is available whenever the integration is loaded."""
        return True

    @property
    def extra_state_attributes(self) -> dict:
        """Return what released apps read: the version, and whether push is set up."""
        token = self._entry.data.get(CONF_PUSH_NOTIFICATION_TOKEN)
        return {
            "integration_version": INTEGRATION_VERSION,
            "push_notifications_configured": isinstance(token, str) and len(token) > 0,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to group entities under a single device."""
        return DeviceInfo(
            identifiers={(DOMAIN, SERVICE_DEVICE_IDENTIFIER)},
            name="Zendo",
            manufacturer="Zendo",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Update the attributes when the push token changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONFIG_UPDATED, self.async_write_ha_state
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on it."""
        super().async_entity_id_changed(old_entity_id)
