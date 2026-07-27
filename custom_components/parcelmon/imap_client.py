"""IMAP access, deliberately scoped to a single folder.

imaplib is blocking, so every call here runs in the executor. The whole security
posture rests on the Gmail filter: carrier mail is labelled, and this client only
ever SELECTs that one folder. It never opens INBOX and never searches across the
account, so a bug here cannot reach the rest of the mailbox.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message

_LOGGER = logging.getLogger(__name__)


class ParcelmonAuthError(Exception):
    """Credentials were rejected."""


class ParcelmonFolderError(Exception):
    """The configured folder does not exist."""

    def __init__(self, folder: str, available: list[str]) -> None:
        self.folder = folder
        self.available = available
        super().__init__(f"Folder {folder!r} not found")


class ParcelmonConnectionError(Exception):
    """The server could not be reached."""


@dataclass(frozen=True, slots=True)
class ImapSettings:
    host: str
    port: int
    username: str
    password: str
    folder: str
    mark_seen: bool = True


def _quote(folder: str) -> str:
    """IMAP folder names with spaces or slashes must be quoted."""
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _folder_names(conn: imaplib.IMAP4_SSL) -> list[str]:
    status, data = conn.list()
    if status != "OK":
        return []
    names: list[str] = []
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        if '"' in line:
            names.append(line.rsplit('"', 2)[-2])
    return sorted(names)


def _login(settings: ImapSettings) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(settings.host, settings.port, timeout=30)
    except OSError as err:
        raise ParcelmonConnectionError(str(err)) from err
    try:
        conn.login(settings.username, settings.password)
    except imaplib.IMAP4.error as err:
        conn.logout()
        raise ParcelmonAuthError(str(err)) from err
    return conn


def list_folders(settings: ImapSettings) -> list[str]:
    """Blocking. Used by the config flow to help the user pick a label."""
    conn = _login(settings)
    try:
        return _folder_names(conn)
    finally:
        conn.logout()


def verify(settings: ImapSettings) -> int:
    """Blocking. Log in, open the folder, report the unread count."""
    conn = _login(settings)
    try:
        status, _ = conn.select(_quote(settings.folder), readonly=True)
        if status != "OK":
            raise ParcelmonFolderError(settings.folder, _folder_names(conn))
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return 0
        return len((data[0] or b"").split())
    finally:
        with contextlib.suppress(imaplib.IMAP4.error):
            conn.close()
        conn.logout()


def fetch_unseen(settings: ImapSettings) -> list[tuple[bytes, Message]]:
    """Blocking. Return (imap_id, message) for every unread message."""
    conn = _login(settings)
    messages: list[tuple[bytes, Message]] = []
    try:
        status, _ = conn.select(_quote(settings.folder), readonly=not settings.mark_seen)
        if status != "OK":
            raise ParcelmonFolderError(settings.folder, _folder_names(conn))

        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []

        for imap_id in (data[0] or b"").split():
            # BODY.PEEK so nothing is marked read until parsing has succeeded.
            status, payload = conn.fetch(imap_id, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                _LOGGER.warning("Could not fetch message %s", imap_id.decode())
                continue
            messages.append((imap_id, email.message_from_bytes(payload[0][1])))
    finally:
        with contextlib.suppress(imaplib.IMAP4.error):
            conn.close()
        conn.logout()
    return messages


def fetch_history(
    settings: ImapSettings, days: int = 0, limit: int = 0
) -> list[tuple[bytes, Message]]:
    """Blocking. Return (imap_id, message) for mail already in the folder.

    Unlike fetch_unseen this ignores the \\Seen flag, so it picks up carrier mail
    that was read before Parcelmon was installed. The folder is opened READ-ONLY
    and messages come back via BODY.PEEK, so a rescan cannot change a flag,
    cannot mark anything read, and is safe to run repeatedly.

    days>0 limits the search to recent mail via IMAP SINCE; limit>0 keeps only
    the newest N matches, since IMAP ids ascend with arrival order.
    """
    conn = _login(settings)
    messages: list[tuple[bytes, Message]] = []
    try:
        status, _ = conn.select(_quote(settings.folder), readonly=True)
        if status != "OK":
            raise ParcelmonFolderError(settings.folder, _folder_names(conn))

        criteria = ["ALL"]
        if days > 0:
            since = (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")
            criteria = ["SINCE", since]

        status, data = conn.search(None, *criteria)
        if status != "OK":
            return []

        imap_ids = (data[0] or b"").split()
        if limit > 0:
            imap_ids = imap_ids[-limit:]

        for imap_id in imap_ids:
            status, payload = conn.fetch(imap_id, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                _LOGGER.warning("Could not fetch message %s", imap_id.decode())
                continue
            messages.append((imap_id, email.message_from_bytes(payload[0][1])))
    finally:
        with contextlib.suppress(imaplib.IMAP4.error):
            conn.close()
        conn.logout()
    return messages


def mark_seen(settings: ImapSettings, imap_ids: list[bytes]) -> None:
    """Blocking. Flag messages read once they have been turned into entities."""
    if not imap_ids or not settings.mark_seen:
        return
    conn = _login(settings)
    try:
        conn.select(_quote(settings.folder))
        for imap_id in imap_ids:
            conn.store(imap_id, "+FLAGS", "\\Seen")
    finally:
        with contextlib.suppress(imaplib.IMAP4.error):
            conn.close()
        conn.logout()
