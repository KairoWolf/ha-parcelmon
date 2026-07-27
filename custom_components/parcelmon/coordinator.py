"""Coordinator: poll the mailbox, parse mail, keep the parcel set current."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_FOLDER,
    CONF_MARK_SEEN,
    CONF_POLL_INTERVAL,
    CONF_RETIRE_DAYS,
    DEFAULT_FOLDER,
    DEFAULT_MARK_SEEN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RETIRE_DAYS,
    DOMAIN,
)
from .imap_client import (
    ImapSettings,
    ParcelmonAuthError,
    ParcelmonConnectionError,
    ParcelmonFolderError,
    fetch_unseen,
    mark_seen,
)
from .models import Parcel
from .parsers import parse_message

_LOGGER = logging.getLogger(__name__)

FINAL_STATES = frozenset({"delivered", "returned"})
MAX_REMEMBERED_MESSAGES = 500


class ParcelmonCoordinator(DataUpdateCoordinator[dict[str, Parcel]]):
    """Holds the current set of parcels, keyed by Parcel.uid."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        options = entry.options
        data = entry.data
        interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval),
            config_entry=entry,
        )

        self.settings = ImapSettings(
            host=data["host"],
            port=data["port"],
            username=data["username"],
            password=data["password"],
            folder=data.get(CONF_FOLDER, DEFAULT_FOLDER),
            mark_seen=options.get(CONF_MARK_SEEN, DEFAULT_MARK_SEEN),
        )
        self.retire_days: int = options.get(CONF_RETIRE_DAYS, DEFAULT_RETIRE_DAYS)

        self._parcels: dict[str, Parcel] = {}
        self._seen_message_ids: list[str] = []
        #: uids retired this cycle, so platforms can drop their entities.
        self.removed: set[str] = set()

    async def _async_update_data(self) -> dict[str, Parcel]:
        try:
            messages = await self.hass.async_add_executor_job(
                fetch_unseen, self.settings
            )
        except ParcelmonAuthError as err:
            raise ConfigEntryAuthFailed(
                "Mailbox rejected the credentials. Gmail App Passwords are "
                "revoked when the account password changes."
            ) from err
        except ParcelmonFolderError as err:
            raise UpdateFailed(
                f"Folder {err.folder!r} not found. Available: "
                f"{', '.join(err.available) or '(none)'}"
            ) from err
        except (ParcelmonConnectionError, OSError) as err:
            raise UpdateFailed(f"Cannot reach {self.settings.host}: {err}") from err

        handled: list[bytes] = []

        for imap_id, message in messages:
            parcel = parse_message(message)
            if parcel is None:
                # Leave unread: either the filter over-matched, or a carrier
                # changed their template and it is worth a human look.
                _LOGGER.warning(
                    "No parcel found in %r from %s",
                    message.get("Subject", "(no subject)"),
                    message.get("From", "?"),
                )
                continue

            if parcel.message_id and parcel.message_id in self._seen_message_ids:
                handled.append(imap_id)
                continue

            self._merge(parcel)
            if parcel.message_id:
                self._seen_message_ids.append(parcel.message_id)
            handled.append(imap_id)
            _LOGGER.debug("Parsed %s -> %s", parcel.uid, parcel.status)

        self._seen_message_ids = self._seen_message_ids[-MAX_REMEMBERED_MESSAGES:]
        self._retire_stale()

        if handled:
            await self.hass.async_add_executor_job(mark_seen, self.settings, handled)

        return dict(self._parcels)

    def _merge(self, parcel: Parcel) -> None:
        """Newer mail wins, but never blank out a field an older email filled in.

        AusPost send several emails per parcel and later ones drop the merchant
        name and ETA. Without this, a delivered notification would wipe the
        sender you actually want in the notification text.
        """
        existing = self._parcels.get(parcel.uid)
        if existing is not None:
            for field in ("sender", "eta", "destination", "photo", "photo_url"):
                if getattr(parcel, field) is None:
                    setattr(parcel, field, getattr(existing, field))
        self._parcels[parcel.uid] = parcel

    def _retire_stale(self) -> None:
        """Drop parcels that finished more than retire_days ago."""
        if self.retire_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.retire_days)
        for uid, parcel in list(self._parcels.items()):
            if parcel.status not in FINAL_STATES:
                continue
            if parcel.seen_at < cutoff:
                del self._parcels[uid]
                self.removed.add(uid)
                _LOGGER.debug("Retired %s", uid)
