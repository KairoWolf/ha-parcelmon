"""Image platform: proof-of-delivery photos.

Only created for parcels that actually carry one. Team Global Express embed the
photo as base64 in the email; Australia Post do not send one at all, so AusPost
parcels never get an image entity.
"""

from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ParcelmonConfigEntry, ParcelmonEntity
from .const import DOMAIN
from .coordinator import ParcelmonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ParcelmonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a photo entity once a parcel turns out to have one."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_photos() -> None:
        new = [
            uid
            for uid, parcel in coordinator.data.items()
            if uid not in known and parcel.has_photo
        ]
        if not new:
            return
        known.update(new)
        async_add_entities(ParcelPhoto(hass, coordinator, uid) for uid in new)

    _add_new_photos()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_photos))


class ParcelPhoto(ParcelmonEntity, ImageEntity):
    """The driver's delivery photo, served from memory."""

    _attr_translation_key = "delivery_photo"
    _attr_content_type = "image/jpeg"

    def __init__(
        self, hass: HomeAssistant, coordinator: ParcelmonCoordinator, uid: str
    ) -> None:
        ParcelmonEntity.__init__(self, coordinator, uid)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{DOMAIN}_{uid}_photo"
        parcel = self.parcel
        self._attr_image_last_updated = parcel.seen_at if parcel else None

    @property
    def available(self) -> bool:
        parcel = self.parcel
        return super().available and parcel is not None and parcel.has_photo

    async def async_image(self) -> bytes | None:
        parcel = self.parcel
        return parcel.photo if parcel else None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Bump the timestamp so the frontend refetches instead of serving a
        # cached photo from the previous delivery attempt.
        parcel = self.parcel
        if parcel is not None and parcel.seen_at != self._attr_image_last_updated:
            self._attr_image_last_updated = parcel.seen_at
        super()._handle_coordinator_update()
