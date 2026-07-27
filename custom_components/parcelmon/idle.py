"""IMAP IDLE watcher: react to carrier mail within seconds instead of polling.

Python's imaplib grew a native idle() only in 3.15, and Home Assistant runs on
3.13, so the command is issued by hand here. The protocol is small: send IDLE,
wait for the "+" continuation, then block on the socket until the server pushes
an untagged EXISTS/RECENT, and send DONE to get back to a normal state.

Everything in this module is blocking and runs on its own thread. It never
touches Home Assistant state directly; the only thing it does on new mail is
schedule a coordinator refresh on the event loop.
"""

from __future__ import annotations

import contextlib
import imaplib
import logging
import socket
import threading
from collections.abc import Callable

from .const import IDLE_RECONNECT_SECONDS, IDLE_RENEW_SECONDS
from .imap_client import ImapSettings, ParcelmonAuthError, _login, _quote

_LOGGER = logging.getLogger(__name__)

#: Untagged responses that mean "the folder changed, go and look".
_WAKE_TOKENS = (b"EXISTS", b"RECENT")


class IdleUnsupportedError(Exception):
    """The server does not advertise the IDLE capability."""


class IdleWatcher:
    """Runs an IDLE loop on a worker thread and calls back on new mail."""

    def __init__(self, settings: ImapSettings, on_mail: Callable[[], None]) -> None:
        self._settings = settings
        self._on_mail = on_mail
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: imaplib.IMAP4_SSL | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="parcelmon-idle", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish and drop the connection under it.

        Shutting the socket down is what unblocks a thread parked in readline();
        without it Home Assistant would wait out the full renew interval.
        """
        self._stop.set()
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError, AttributeError):
                conn.sock.shutdown(socket.SHUT_RDWR)
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._idle_session()
            except ParcelmonAuthError:
                # Credentials will not fix themselves; polling stays as the
                # safety net and reauth is raised by the coordinator.
                _LOGGER.error("Push disabled: the mailbox rejected the credentials")
                return
            except IdleUnsupportedError:
                _LOGGER.warning(
                    "Push disabled: %s does not support IMAP IDLE. "
                    "Parcelmon will keep checking on the timed interval.",
                    self._settings.host,
                )
                return
            except (imaplib.IMAP4.error, OSError) as err:
                if self._stop.is_set():
                    return
                _LOGGER.debug("IDLE dropped (%s); reconnecting", err)
            finally:
                self._close()

            # Wait before reconnecting, but wake immediately on shutdown.
            self._stop.wait(IDLE_RECONNECT_SECONDS)

    def _idle_session(self) -> None:
        """One connection's worth of IDLE, renewed until it fails or stops."""
        conn = _login(self._settings)
        self._conn = conn

        if not any(b"IDLE" in cap.upper() for cap in conn.capabilities):
            raise IdleUnsupportedError(self._settings.host)

        status, _ = conn.select(_quote(self._settings.folder), readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Cannot select {self._settings.folder!r}")

        _LOGGER.debug("IDLE established on %s", self._settings.folder)
        while not self._stop.is_set():
            if self._idle_once(conn):
                # Leave IDLE before the refresh so the coordinator's own
                # connection is not racing this one on the same folder.
                self._on_mail()

    def _idle_once(self, conn: imaplib.IMAP4_SSL) -> bool:
        """Idle until the folder changes or the renew timer expires.

        Returns True if the server reported new mail.
        """
        tag = conn._new_tag()
        conn.send(b"%s IDLE\r\n" % tag)

        if not conn.readline().startswith(b"+"):
            raise imaplib.IMAP4.error("Server refused IDLE")

        conn.sock.settimeout(IDLE_RENEW_SECONDS)
        got_mail = False
        try:
            while True:
                line = conn.readline()
                if not line:
                    raise imaplib.IMAP4.abort("Connection closed during IDLE")
                if any(token in line.upper() for token in _WAKE_TOKENS):
                    got_mail = True
                    break
        except TimeoutError:
            pass  # renew window elapsed with nothing new; that is normal
        finally:
            conn.sock.settimeout(None)
            try:
                conn.send(b"DONE\r\n")
                conn._get_tagged_response(tag)
            except (imaplib.IMAP4.error, OSError):
                if not self._stop.is_set():
                    raise

        return got_mail

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()
