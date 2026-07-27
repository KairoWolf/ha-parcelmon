"""Persist tracked parcels across restarts.

Mail is marked read once it has been parsed, so a parcel held only in memory is
gone for good after a restart: the message will never come back from an UNSEEN
search, and the parcel silently disappears from the dashboard while it is still
in transit. This keeps the coordinator's state on disk instead.

Serialisation lives here rather than on Parcel because the model is driven by
the carrier fixtures and is deliberately left alone. Fields are discovered from
the dataclass, so adding one there does not need a change here.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import fields
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .models import Parcel

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 10  # seconds; batches the writes a 5-minute poll would cause

#: Fields needing more than plain JSON.
_BYTES_FIELDS = frozenset({"photo"})
_DATETIME_FIELDS = frozenset({"email_date", "seen_at"})


def _field_names() -> frozenset[str]:
    return frozenset(f.name for f in fields(Parcel))


def parcel_to_dict(parcel: Parcel) -> dict[str, Any]:
    """JSON-safe view of a parcel, photo bytes included."""
    data: dict[str, Any] = {}
    for name in _field_names():
        value = getattr(parcel, name)
        if value is None:
            data[name] = None
        elif name in _BYTES_FIELDS:
            data[name] = base64.b64encode(value).decode("ascii")
        elif name in _DATETIME_FIELDS:
            data[name] = value.isoformat()
        else:
            data[name] = value
    return data


def parcel_from_dict(data: dict[str, Any]) -> Parcel | None:
    """Rebuild a parcel, or None if the record is unusable.

    Unknown keys are dropped and unparseable values fall back to the dataclass
    default, so a stored file written by a different version cannot break setup.
    """
    known = _field_names()
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        if name not in known or value is None:
            continue
        try:
            if name in _BYTES_FIELDS:
                kwargs[name] = base64.b64decode(value)
            elif name in _DATETIME_FIELDS:
                kwargs[name] = datetime.fromisoformat(value)
            else:
                kwargs[name] = value
        except (ValueError, TypeError):
            _LOGGER.debug("Dropping unreadable stored field %r", name)

    if not kwargs.get("carrier") or not kwargs.get("tracking"):
        return None
    try:
        return Parcel(**kwargs)
    except TypeError:
        _LOGGER.warning("Discarding stored parcel that no longer fits the model")
        return None


class ParcelmonStore:
    """Reads and writes one config entry's parcels."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        # private=True: the file holds tracking numbers, delivery addresses and
        # photographs of someone's front door.
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True
        )

    async def async_load(self) -> tuple[dict[str, Parcel], list[str], set[str]]:
        """Return the stored parcels, handled Message-IDs and manual uids."""
        try:
            data = await self._store.async_load()
        except (HomeAssistantError, ValueError, TypeError) as err:
            # A corrupt store must not block setup: an empty start recovers on
            # the next poll, an aborted setup would need manual intervention.
            _LOGGER.warning("Could not read stored parcels, starting empty: %s", err)
            return {}, [], set()

        if not data:
            return {}, [], set()

        parcels: dict[str, Parcel] = {}
        for record in data.get("parcels", []):
            parcel = parcel_from_dict(record)
            if parcel is not None:
                parcels[parcel.uid] = parcel

        seen = [str(mid) for mid in data.get("seen_message_ids", []) if mid]
        manual = {str(uid) for uid in data.get("manual", []) if uid}
        _LOGGER.debug("Restored %s parcels from storage", len(parcels))
        return parcels, seen, manual

    @callback
    def async_schedule_save(
        self,
        parcels: dict[str, Parcel],
        seen_message_ids: list[str],
        manual: set[str] | None = None,
    ) -> None:
        """Queue a debounced write of the current state."""
        snapshot = {
            "parcels": [parcel_to_dict(p) for p in parcels.values()],
            "seen_message_ids": list(seen_message_ids),
            "manual": sorted(manual or ()),
        }
        self._store.async_delay_save(lambda: snapshot, SAVE_DELAY)

    async def async_remove(self) -> None:
        """Delete the file when the config entry is removed."""
        await self._store.async_remove()
