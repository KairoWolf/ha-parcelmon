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
    ATTR_CARRIER,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    ATTR_ETA,
    ATTR_LIMIT,
    ATTR_SENDER,
    ATTR_STATUS,
    ATTR_TRACKING,
    CARRIER_LABELS,
    DEFAULT_RESCAN_DAYS,
    DEFAULT_RESCAN_LIMIT,
    DOMAIN,
    MAX_RESCAN_DAYS,
    MAX_RESCAN_LIMIT,
    SERVICE_ADD_PARCEL,
    SERVICE_CLEAR_DELIVERED,
    SERVICE_GET_PARCELS,
    SERVICE_REFRESH,
    SERVICE_REMOVE_PARCEL,
    SERVICE_RESCAN,
    SERVICE_SET_STATUS,
)
from .coordinator import ParcelmonCoordinator
from .models import STATUSES, UNKNOWN, Parcel
from .store import ParcelmonStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.BUTTON,
]

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

ADD_PARCEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING): vol.All(cv.string, vol.Length(min=3, max=64)),
        vol.Optional(ATTR_CARRIER, default="auspost"): vol.In(list(CARRIER_LABELS)),
        vol.Optional(ATTR_STATUS, default=UNKNOWN): vol.In(list(STATUSES)),
        vol.Optional(ATTR_SENDER): cv.string,
        vol.Optional(ATTR_ETA): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)

REMOVE_PARCEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)

SET_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING): cv.string,
        vol.Required(ATTR_STATUS): vol.In(list(STATUSES)),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)

ENTRY_ONLY_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

type ParcelmonConfigEntry = ConfigEntry[ParcelmonCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Set up Parcelmon from a config entry."""
    coordinator = ParcelmonCoordinator(hass, entry)
    # Before the first poll: mail is marked read once parsed, so parcels that
    # only lived in memory could never be recovered from the mailbox.
    await coordinator.async_restore()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    _async_register_services(hass)

    # Push last: the folder is only watched once the platforms can react to it.
    coordinator.async_start_push()
    entry.async_on_unload(coordinator.async_stop_push)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and not _loaded_entries(hass, exclude=entry.entry_id):
        for service in (
            SERVICE_RESCAN,
            SERVICE_ADD_PARCEL,
            SERVICE_REMOVE_PARCEL,
            SERVICE_SET_STATUS,
            SERVICE_CLEAR_DELIVERED,
            SERVICE_REFRESH,
            SERVICE_GET_PARCELS,
        ):
            hass.services.async_remove(DOMAIN, service)
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


def _resolve_entries(
    hass: HomeAssistant, call: ServiceCall
) -> list[ParcelmonConfigEntry]:
    """Which mailboxes a service call applies to, or a user-facing error."""
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
        return [entry]

    entries = _loaded_entries(hass)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )
    return entries


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the rescan action once, however many mailboxes are configured."""
    if hass.services.has_service(DOMAIN, SERVICE_RESCAN):
        return

    async def _async_rescan(call: ServiceCall) -> ServiceResponse:
        """Sweep the folder for parcels in mail that was already read."""
        totals = {"scanned": 0, "matched": 0, "new_parcels": 0, "tracked": 0}
        for entry in _resolve_entries(hass, call):
            result = await entry.runtime_data.async_rescan(
                days=call.data[ATTR_DAYS], limit=call.data[ATTR_LIMIT]
            )
            for key, value in result.items():
                totals[key] += value
        return totals

    async def _async_add_parcel(call: ServiceCall) -> ServiceResponse:
        """Track a parcel by hand when its email never arrives or won't parse."""
        entries = _resolve_entries(hass, call)
        uid = await entries[0].runtime_data.async_add_manual_parcel(
            tracking=call.data[ATTR_TRACKING],
            carrier=call.data[ATTR_CARRIER],
            status=call.data[ATTR_STATUS],
            sender=call.data.get(ATTR_SENDER),
            eta=call.data.get(ATTR_ETA),
        )
        return {"uid": uid}

    async def _async_remove_parcel(call: ServiceCall) -> ServiceResponse:
        """Stop tracking a parcel, by tracking number or uid."""
        removed: list[str] = []
        for entry in _resolve_entries(hass, call):
            uid = await entry.runtime_data.async_remove_parcel(call.data[ATTR_TRACKING])
            if uid is not None:
                removed.append(uid)
        if not removed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="parcel_not_found",
                translation_placeholders={"target": call.data[ATTR_TRACKING]},
            )
        return {"removed": removed}

    async def _async_set_status(call: ServiceCall) -> ServiceResponse:
        """Correct a parcel's status when the carrier's wording defeated us."""
        for entry in _resolve_entries(hass, call):
            uid = await entry.runtime_data.async_set_status(
                call.data[ATTR_TRACKING], call.data[ATTR_STATUS]
            )
            if uid is not None:
                return {"uid": uid, "status": call.data[ATTR_STATUS]}
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="parcel_not_found",
            translation_placeholders={"target": call.data[ATTR_TRACKING]},
        )

    async def _async_clear_delivered(call: ServiceCall) -> ServiceResponse:
        """Drop finished parcels now rather than waiting out retire_days."""
        cleared = 0
        for entry in _resolve_entries(hass, call):
            cleared += await entry.runtime_data.async_clear_delivered()
        return {"cleared": cleared}

    async def _async_refresh(call: ServiceCall) -> None:
        """Read the mailbox now instead of waiting for the next interval."""
        for entry in _resolve_entries(hass, call):
            await entry.runtime_data.async_refresh()

    async def _async_get_parcels(call: ServiceCall) -> ServiceResponse:
        """Return every tracked parcel, for templates and dashboards."""
        parcels = []
        for entry in _resolve_entries(hass, call):
            coordinator = entry.runtime_data
            parcels.extend(
                parcel.attributes() | {"uid": uid, "manual": uid in coordinator.manual}
                for uid, parcel in sorted(
                    coordinator.data.items(),
                    key=lambda kv: kv[1].seen_at,
                    reverse=True,
                )
            )
        return {"parcels": parcels, "count": len(parcels)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESCAN,
        _async_rescan,
        schema=RESCAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PARCEL,
        _async_add_parcel,
        schema=ADD_PARCEL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_PARCEL,
        _async_remove_parcel,
        schema=REMOVE_PARCEL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_STATUS,
        _async_set_status,
        schema=SET_STATUS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_DELIVERED,
        _async_clear_delivered,
        schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=ENTRY_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PARCELS,
        _async_get_parcels,
        schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_reload_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> None:
    """Reload when options change, so the poll interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ParcelmonConfigEntry) -> None:
    """Delete stored parcels when the mailbox is removed."""
    await ParcelmonStore(hass, entry.entry_id).async_remove()


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
