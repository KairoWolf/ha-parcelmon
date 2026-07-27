"""The Parcelmon integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    ATTR_LIMIT,
    CARRIER_LABELS,
    DEFAULT_RESCAN_DAYS,
    DEFAULT_RESCAN_LIMIT,
    DOMAIN,
    MAX_RESCAN_DAYS,
    MAX_RESCAN_LIMIT,
    SERVICE_RESCAN,
)
from .coordinator import ParcelmonCoordinator
from .models import Parcel

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE, Platform.BUTTON]

RESCAN_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_DAYS, default=DEFAULT_RESCAN_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_RESCAN_DAYS)
        ),
        vol.Optional(ATTR_LIMIT, default=DEFAULT_RESCAN_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_RESCAN_LIMIT)
        ),
    }
)

type ParcelmonConfigEntry = ConfigEntry[ParcelmonCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Set up Parcelmon from a config entry."""
    coordinator = ParcelmonCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and not _loaded_entries(hass, exclude=entry.entry_id):
        hass.services.async_remove(DOMAIN, SERVICE_RESCAN)
    return unloaded


def _loaded_entries(
    hass: HomeAssistant, exclude: str | None = None
) -> list[ParcelmonConfigEntry]:
    """Every Parcelmon entry currently loaded, optionally minus one."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and entry.entry_id != exclude
    ]


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the rescan action once, however many mailboxes are configured."""
    if hass.services.has_service(DOMAIN, SERVICE_RESCAN):
        return

    async def _async_rescan(call: ServiceCall) -> ServiceResponse:
        """Sweep the folder for parcels in mail that was already read."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id is not None:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="entry_not_found",
                    translation_placeholders={"target": entry_id},
                )
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="entry_not_loaded",
                    translation_placeholders={"target": entry.title},
                )
            entries = [entry]
        else:
            entries = _loaded_entries(hass)
            if not entries:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="no_entries"
                )

        totals = {"scanned": 0, "matched": 0, "new_parcels": 0, "tracked": 0}
        for entry in entries:
            result = await entry.runtime_data.async_rescan(
                days=call.data[ATTR_DAYS], limit=call.data[ATTR_LIMIT]
            )
            for key, value in result.items():
                totals[key] += value
        return totals

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESCAN,
        _async_rescan,
        schema=RESCAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


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
