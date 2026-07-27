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
    DEFAULT_RESCAN_DAYS,
    DEFAULT_RESCAN_LIMIT,
    DEFAULT_RETIRE_DAYS,
    DOMAIN,
)
from .imap_client import (
    ImapSettings,
    ParcelmonAuthError,
    ParcelmonConnectionError,
    ParcelmonFolderError,
    fetch_history,
    fetch_unseen,
    mark_seen,
)
from .models import Parcel
from .parsers import parse_message
from .store import ParcelmonStore

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
        self.store = ParcelmonStore(hass, entry.entry_id)

    async def async_restore(self) -> None:
        """Load parcels saved before the last shutdown.

        Must run before the first refresh. Mail is marked read once parsed, so
        without this a restart loses every in-flight parcel permanently: the
        message will never appear in an UNSEEN search again.
        """
        self._parcels, self._seen_message_ids = await self.store.async_load()
        # Anything that finished while Home Assistant was down should not come
        # back just because it was on disk.
        self._retire_stale()
        self.removed.clear()

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

        # Save before returning: the messages behind these parcels have just
        # been marked read and cannot be fetched a second time.
        self.store.async_schedule_save(self._parcels, self._seen_message_ids)
        return dict(self._parcels)

    async def async_rescan(
        self, days: int = DEFAULT_RESCAN_DAYS, limit: int = DEFAULT_RESCAN_LIMIT
    ) -> dict[str, int]:
        """Backfill parcels from mail already sitting in the folder, read or not.

        Routine polling only ever looks at UNSEEN mail, so anything the user (or
        a previous run with mark_seen on) already read is invisible to it. This
        reads the folder read-only instead, which means it changes no flags and
        can be run as often as you like.
        """
        try:
            messages = await self.hass.async_add_executor_job(
                fetch_history, self.settings, days, limit
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

        before = set(self._parcels)
        matched = 0

        for _imap_id, message in messages:
            parcel = parse_message(message)
            if parcel is None:
                # Quietly skipped: a rescan sweeps the whole folder, so unmatched
                # mail here is normal and must not spam the log the way a live
                # poll's unmatched message does.
                continue

            matched += 1
            if parcel.message_id and parcel.message_id in self._seen_message_ids:
                continue

            # Date the parcel by its email, not by now, so retire_days measures
            # from when the parcel actually finished rather than from the rescan.
            if parcel.email_date is not None:
                parcel.seen_at = parcel.email_date

            existing = self._parcels.get(parcel.uid)
            if existing is not None and existing.seen_at > parcel.seen_at:
                # Live polling already knows something newer about this parcel.
                continue

            self._merge(parcel)
            if parcel.message_id:
                self._seen_message_ids.append(parcel.message_id)

        self._seen_message_ids = self._seen_message_ids[-MAX_REMEMBERED_MESSAGES:]
        self._retire_stale()

        result = {
            "scanned": len(messages),
            "matched": matched,
            "new_parcels": len(set(self._parcels) - before),
            "tracked": len(self._parcels),
        }
        _LOGGER.debug("Rescan over %s days: %s", days or "all", result)
        self.store.async_schedule_save(self._parcels, self._seen_message_ids)
        self.async_set_updated_data(dict(self._parcels))
        return result

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
