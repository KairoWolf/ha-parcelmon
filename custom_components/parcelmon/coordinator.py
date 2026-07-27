"""Coordinator: poll the mailbox, parse mail, keep the parcel set current."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_FOLDER,
    CONF_MARK_SEEN,
    CONF_POLL_INTERVAL,
    CONF_PUSH,
    CONF_RETIRE_DAYS,
    DEFAULT_FOLDER,
    DEFAULT_MARK_SEEN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PUSH,
    DEFAULT_RESCAN_DAYS,
    DEFAULT_RESCAN_LIMIT,
    DEFAULT_RETIRE_DAYS,
    DOMAIN,
    EVENT_PARCEL_UPDATE,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PUSH_FALLBACK_INTERVAL,
    TRACKING_URLS,
)
from .idle import IdleWatcher
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
from .parsers import parse_message_all
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
        self.push_enabled: bool = options.get(CONF_PUSH, DEFAULT_PUSH)

        # With push on, IDLE carries the news and the timer is only a backstop
        # for a connection that died without saying so.
        interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        interval = min(max(interval, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)
        if self.push_enabled:
            interval = PUSH_FALLBACK_INTERVAL

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
        #: uids added by hand, so a later real email is always allowed to win.
        self.manual: set[str] = set()
        self.store = ParcelmonStore(hass, entry.entry_id)
        self._watcher: IdleWatcher | None = None
        #: When the mailbox was last read successfully, for the diagnostic sensor.
        self.last_checked: datetime | None = None

    @callback
    def async_start_push(self) -> None:
        """Begin watching the folder with IMAP IDLE, if push is enabled."""
        if not self.push_enabled or self._watcher is not None:
            return
        self._watcher = IdleWatcher(self.settings, self._on_new_mail)
        self._watcher.start()

    @callback
    def async_stop_push(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.stop()

    def _on_new_mail(self) -> None:
        """Called from the IDLE thread; hop back onto the event loop."""
        _LOGGER.debug("Push: mailbox reported new mail")
        self.hass.add_job(self.async_request_refresh)

    async def async_restore(self) -> None:
        """Load parcels saved before the last shutdown.

        Must run before the first refresh. Mail is marked read once parsed, so
        without this a restart loses every in-flight parcel permanently: the
        message will never appear in an UNSEEN search again.
        """
        self._parcels, self._seen_message_ids, self.manual = await self.store.async_load()
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
            parcels = parse_message_all(message)
            if not parcels:
                # Leave unread: either the filter over-matched, or a carrier
                # changed their template and it is worth a human look.
                _LOGGER.warning(
                    "No parcel found in %r from %s",
                    message.get("Subject", "(no subject)"),
                    message.get("From", "?"),
                )
                continue

            if any(
                p.message_id and p.message_id in self._seen_message_ids
                for p in parcels
            ):
                handled.append(imap_id)
                continue

            for parcel in parcels:
                previous = self._parcels.get(parcel.uid)
                self._merge(parcel)
                self._fire_update(self._parcels[parcel.uid], previous)
                if parcel.message_id:
                    self._seen_message_ids.append(parcel.message_id)
                _LOGGER.debug("Parsed %s -> %s", parcel.uid, parcel.status)
            if len(parcels) > 1:
                _LOGGER.debug(
                    "%s carried %s consignments", imap_id.decode(), len(parcels)
                )
            handled.append(imap_id)

        self._seen_message_ids = self._seen_message_ids[-MAX_REMEMBERED_MESSAGES:]
        self._retire_stale()

        if handled:
            await self.hass.async_add_executor_job(mark_seen, self.settings, handled)

        self.last_checked = datetime.now(UTC)

        # Save before returning: the messages behind these parcels have just
        # been marked read and cannot be fetched a second time.
        self.store.async_schedule_save(
            self._parcels, self._seen_message_ids, self.manual
        )
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
            parsed = parse_message_all(message)
            if not parsed:
                # Quietly skipped: a rescan sweeps the whole folder, so unmatched
                # mail here is normal and must not spam the log the way a live
                # poll's unmatched message does.
                continue

            matched += 1
            if any(
                p.message_id and p.message_id in self._seen_message_ids
                for p in parsed
            ):
                continue

            for parcel in parsed:
                # Date the parcel by its email, not by now, so retire_days
                # measures from when the parcel actually finished rather than
                # from the rescan.
                if parcel.email_date is not None:
                    parcel.seen_at = parcel.email_date

                existing = self._parcels.get(parcel.uid)
                if (
                    existing is not None
                    and existing.seen_at > parcel.seen_at
                    and parcel.uid not in self.manual
                ):
                    # Live polling already knows something newer about this
                    # parcel. A hand-added placeholder is the exception: real
                    # mail always supersedes it, however recently it was typed in.
                    continue
                self.manual.discard(parcel.uid)

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
        self.store.async_schedule_save(
            self._parcels, self._seen_message_ids, self.manual
        )
        self.async_set_updated_data(dict(self._parcels))
        return result

    @callback
    def _fire_update(self, parcel: Parcel, previous: Parcel | None) -> None:
        """Announce a new parcel or a status change on the bus.

        Only live polling calls this. A rescan sweeps historical mail, so firing
        there would re-announce months of deliveries and set off every
        notification automation at once.
        """
        if previous is not None and previous.status == parcel.status:
            return
        self.hass.bus.async_fire(
            EVENT_PARCEL_UPDATE,
            {
                "uid": parcel.uid,
                "carrier": parcel.carrier,
                "tracking_number": parcel.tracking,
                "status": parcel.status,
                "previous_status": previous.status if previous else None,
                "status_text": parcel.status_text,
                "sender": parcel.sender,
                "eta": parcel.eta,
                "tracking_url": parcel.tracking_url,
                "has_photo": parcel.has_photo,
            },
        )

    async def async_add_manual_parcel(
        self,
        tracking: str,
        carrier: str,
        status: str,
        sender: str | None = None,
        eta: str | None = None,
    ) -> str:
        """Track a parcel by hand, for mail that never arrives or won't parse.

        Returns the uid. If a real email turns up later it takes over the same
        uid, so the manual entry is replaced rather than duplicated.
        """
        parcel = Parcel(
            carrier=carrier,
            tracking=tracking.strip().upper(),
            status=status,
            sender=sender,
            eta=eta,
            tracking_url=TRACKING_URLS.get(carrier, "").format(tracking.strip().upper())
            or None,
        )
        previous = self._parcels.get(parcel.uid)
        self._merge(parcel)
        self.manual.add(parcel.uid)
        self._fire_update(self._parcels[parcel.uid], previous)
        self.store.async_schedule_save(
            self._parcels, self._seen_message_ids, self.manual
        )
        self.async_set_updated_data(dict(self._parcels))
        _LOGGER.debug("Manually added %s", parcel.uid)
        return parcel.uid

    def find(self, tracking: str) -> Parcel | None:
        """Look a parcel up by uid or tracking number, case-insensitively."""
        needle = tracking.strip().lower()
        for uid, parcel in self._parcels.items():
            if uid.lower() == needle or parcel.tracking.lower() == needle:
                return parcel
        return None

    async def async_set_status(self, tracking: str, status: str) -> str | None:
        """Correct a parcel's status by hand, firing the usual change event.

        Useful when a carrier's wording defeats the classifier, or when a parcel
        turned up on the doorstep without a delivery email ever arriving.
        """
        parcel = self.find(tracking)
        if parcel is None:
            return None

        previous = Parcel(**{f: getattr(parcel, f) for f in ("carrier", "tracking")})
        previous.status = parcel.status
        if parcel.status == status:
            return parcel.uid

        parcel.status = status
        parcel.seen_at = datetime.now(UTC)
        self._fire_update(parcel, previous)
        self.store.async_schedule_save(
            self._parcels, self._seen_message_ids, self.manual
        )
        self.async_set_updated_data(dict(self._parcels))
        _LOGGER.debug("Set %s -> %s by hand", parcel.uid, status)
        return parcel.uid

    async def async_clear_delivered(self) -> int:
        """Drop every finished parcel now, ignoring retire_days."""
        gone = [uid for uid, p in self._parcels.items() if p.status in FINAL_STATES]
        for uid in gone:
            del self._parcels[uid]
            self.manual.discard(uid)
            self.removed.add(uid)
        if gone:
            self.store.async_schedule_save(
                self._parcels, self._seen_message_ids, self.manual
            )
            self.async_set_updated_data(dict(self._parcels))
        _LOGGER.debug("Cleared %s finished parcels", len(gone))
        return len(gone)

    async def async_remove_parcel(self, tracking: str) -> str | None:
        """Stop tracking a parcel, by uid or by tracking number."""
        needle = tracking.strip().lower()
        for uid, parcel in list(self._parcels.items()):
            if uid.lower() == needle or parcel.tracking.lower() == needle:
                del self._parcels[uid]
                self.manual.discard(uid)
                self.removed.add(uid)
                self.store.async_schedule_save(
                    self._parcels, self._seen_message_ids, self.manual
                )
                self.async_set_updated_data(dict(self._parcels))
                _LOGGER.debug("Removed %s", uid)
                return uid
        return None

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
