"""Sensor platform: one status sensor per parcel, plus an account summary."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ParcelmonConfigEntry, ParcelmonEntity
from .const import DOMAIN, STATUS_ICONS
from .coordinator import ParcelmonCoordinator
from .models import STATUSES

_LOGGER = logging.getLogger(__name__)

ACTIVE_STATES = ("in_transit", "out_for_delivery", "attempted", "awaiting_collection")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ParcelmonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the summary sensor and track parcels as they appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_parcels() -> None:
        new = [uid for uid in coordinator.data if uid not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(ParcelStatusSensor(coordinator, uid) for uid in new)

    async_add_entities([ParcelSummarySensor(coordinator, entry)])
    _add_new_parcels()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_parcels))


class ParcelStatusSensor(ParcelmonEntity, SensorEntity):
    """Current status of one parcel."""

    _attr_translation_key = "parcel_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = list(STATUSES)

    def __init__(self, coordinator: ParcelmonCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid)
        self._attr_unique_id = f"{DOMAIN}_{uid}_status"

    @property
    def native_value(self) -> StateType:
        parcel = self.parcel
        return parcel.status if parcel else None

    @property
    def icon(self) -> str:
        parcel = self.parcel
        return STATUS_ICONS.get(parcel.status if parcel else "unknown", STATUS_ICONS["unknown"])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        parcel = self.parcel
        return parcel.attributes() if parcel else None


class ParcelSummarySensor(CoordinatorEntity[ParcelmonCoordinator], SensorEntity):
    """How many parcels are currently on their way."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcels_active"
    _attr_icon = "mdi:package-variant-closed"
    _attr_native_unit_of_measurement = "parcels"

    def __init__(self, coordinator: ParcelmonCoordinator, entry: ParcelmonConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Parcelmon",
            manufacturer="Parcelmon",
            model="Mailbox watcher",
            entry_type="service",
        )

    @property
    def native_value(self) -> int:
        return sum(
            1 for parcel in self.coordinator.data.values() if parcel.status in ACTIVE_STATES
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parcels = list(self.coordinator.data.values())
        counts: dict[str, int] = {}
        for parcel in parcels:
            counts[parcel.status] = counts.get(parcel.status, 0) + 1
        return {
            "total_tracked": len(parcels),
            "by_status": counts,
            "folder": self.coordinator.settings.folder,
            "parcels": [
                {
                    "tracking_number": parcel.tracking,
                    "carrier": parcel.carrier,
                    "status": parcel.status,
                    "sender": parcel.sender,
                    "eta": parcel.eta,
                }
                for parcel in sorted(parcels, key=lambda p: p.seen_at, reverse=True)
            ],
        }
