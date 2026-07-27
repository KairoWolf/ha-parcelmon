"""The Parcelmon integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CARRIER_LABELS, DOMAIN
from .coordinator import ParcelmonCoordinator
from .models import Parcel

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE]

type ParcelmonConfigEntry = ConfigEntry[ParcelmonCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Set up Parcelmon from a config entry."""
    coordinator = ParcelmonCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> None:
    """Reload when options change, so the poll interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


class ParcelmonEntity(CoordinatorEntity[ParcelmonCoordinator]):
    """Shared base: one HA device per parcel."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ParcelmonCoordinator, uid: str) -> None:
        super().__init__(coordinator)
        self._uid = uid

    @property
    def parcel(self) -> Parcel | None:
        return self.coordinator.data.get(self._uid)

    @property
    def available(self) -> bool:
        return super().available and self.parcel is not None

    @property
    def device_info(self) -> DeviceInfo:
        parcel = self.parcel
        carrier = parcel.carrier if parcel else "unknown"
        tracking = parcel.tracking if parcel else self._uid
        sender = parcel.sender if parcel else None
        name = f"{sender} \u2013 {tracking}" if sender else tracking
        return DeviceInfo(
            identifiers={(DOMAIN, self._uid)},
            name=f"Parcel {name}",
            manufacturer=CARRIER_LABELS.get(carrier, carrier),
            model="Parcel",
            configuration_url=parcel.tracking_url if parcel else None,
        )
