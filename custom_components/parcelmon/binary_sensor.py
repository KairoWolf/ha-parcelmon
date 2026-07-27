"""Binary sensor platform: whether the mailbox is actually being read.

Worth surfacing as an entity rather than only a log line: a revoked App Password
or a renamed label leaves Parcelmon silently reporting the parcels it already
knows about, which looks identical to "no parcels have arrived".
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ParcelmonConfigEntry
from .const import DOMAIN
from .coordinator import ParcelmonCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ParcelmonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ParcelmonConnectivity(entry.runtime_data, entry)])


class ParcelmonConnectivity(
    CoordinatorEntity[ParcelmonCoordinator], BinarySensorEntity
):
    """On while the last mailbox check succeeded."""

    _attr_has_entity_name = True
    _attr_translation_key = "mailbox"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ParcelmonCoordinator, entry: ParcelmonConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_mailbox"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Parcelmon",
            manufacturer="Parcelmon",
            model="Mailbox watcher",
            entry_type="service",
        )

    @property
    def available(self) -> bool:
        # Deliberately always available: an entity that goes unavailable when
        # the connection drops cannot report that the connection has dropped.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        coordinator = self.coordinator
        return {
            "host": coordinator.settings.host,
            "folder": coordinator.settings.folder,
            "push_enabled": coordinator.push_enabled,
            "update_interval": str(coordinator.update_interval),
        }
