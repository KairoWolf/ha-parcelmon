"""Button platform: a one-press rescan of mail already in the folder.

Routine polling only sees UNSEEN mail. This button is the manual escape hatch
for the case that catches everyone on first install: the Gmail label is full of
carrier email that has already been read, so nothing shows up until the next
parcel is posted.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ParcelmonConfigEntry
from .const import DOMAIN
from .coordinator import ParcelmonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ParcelmonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the mailbox buttons to the Parcelmon service device."""
    async_add_entities(
        [
            ParcelmonCheckNowButton(entry.runtime_data, entry),
            ParcelmonRescanButton(entry.runtime_data, entry),
        ]
    )


class _ParcelmonButton(CoordinatorEntity[ParcelmonCoordinator], ButtonEntity):
    """Shared wiring: every button belongs to the one service device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ParcelmonCoordinator, entry: ParcelmonConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Parcelmon",
            manufacturer="Parcelmon",
            model="Mailbox watcher",
            entry_type="service",
        )


class ParcelmonCheckNowButton(_ParcelmonButton):
    """Read the mailbox now instead of waiting for the next interval."""

    _attr_translation_key = "check_now"
    _attr_icon = "mdi:email-arrow-left-outline"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "check_now")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class ParcelmonRescanButton(_ParcelmonButton):
    """Rescan the folder for parcels in mail that was already read."""

    _attr_translation_key = "rescan"
    _attr_icon = "mdi:email-sync-outline"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "rescan")

    async def async_press(self) -> None:
        """Rescan using the defaults. Use the action for a wider window."""
        result = await self.coordinator.async_rescan()
        _LOGGER.info(
            "Rescan found %s parcel emails in %s messages, %s new",
            result["matched"],
            result["scanned"],
            result["new_parcels"],
        )
