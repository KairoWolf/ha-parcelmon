"""Diagnostics support for Parcelmon."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ParcelmonConfigEntry

# Tracking numbers and street-level detail are personal; the photo is a picture
# of someone's front door. None of it belongs in a pasted diagnostics dump.
REDACT_ENTRY = {"password", "username"}
REDACT_PARCEL = {"tracking_number", "tracking_url", "photo_url", "destination", "subject"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ParcelmonConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "folder": coordinator.settings.folder,
            "host": coordinator.settings.host,
            "retire_days": coordinator.retire_days,
            "parcel_count": len(coordinator.data or {}),
        },
        "parcels": [
            async_redact_data(parcel.attributes(), REDACT_PARCEL)
            | {"photo_bytes": len(parcel.photo) if parcel.photo else 0}
            for parcel in (coordinator.data or {}).values()
        ],
    }
