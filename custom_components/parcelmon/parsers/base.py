"""Shared plumbing for carrier parsers.

Handles the bits that are the same regardless of carrier: pulling the HTML part
out of a MIME message (AusPost sends a bare text/html body, Team Global Express
sends multipart/mixed with a quoted-printable text/HTML part), decoding it, and
normalising whitespace so downstream regexes are not fighting &nbsp; and
soft line breaks.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import Message
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from ..models import Parcel

# Some mail gateways rewrite attributes to defang them (we have seen
# defang_name=, defang_contenteditable= in real Team Global Express mail).
_DEFANG = re.compile(r"\bdefang_([a-zA-Z-]+)=")
_WS = re.compile(r"[\s\u00a0]+")


def squash(text: str | None) -> str:
    """Collapse all whitespace, including non-breaking spaces, to single spaces."""
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


def html_part(msg: Message) -> str:
    """Return the decoded text/html body, or '' if there isn't one.

    Prefers text/html; falls back to text/plain wrapped so BeautifulSoup can
    still be pointed at it without special-casing upstream.
    """
    html_candidates: list[str] = []
    text_candidates: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/html", "text/plain"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        (html_candidates if ctype == "text/html" else text_candidates).append(decoded)

    if html_candidates:
        return _DEFANG.sub(r"\1=", max(html_candidates, key=len))
    if text_candidates:
        return "<pre>" + max(text_candidates, key=len) + "</pre>"
    return ""


def soup_of(msg: Message) -> BeautifulSoup:
    return BeautifulSoup(html_part(msg), "html.parser")


def visible_text(soup: BeautifulSoup) -> str:
    """All rendered text as one normalised line."""
    for tag in soup(["style", "script", "title"]):
        tag.decompose()
    return squash(soup.get_text(" "))


def header_date(msg: Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


class CarrierParser:
    """Subclass per carrier."""

    carrier: str = "unknown"
    #: Substrings matched (case-insensitively) against the From header.
    from_domains: tuple[str, ...] = ()

    def matches(self, msg: Message) -> bool:
        sender = (msg.get("From") or "").lower()
        return any(d in sender for d in self.from_domains)

    def parse(self, msg: Message) -> Parcel | None:  # pragma: no cover - interface
        raise NotImplementedError

    def parse_all(self, msg: Message) -> list[Parcel]:
        """Every parcel in one email.

        Carriers usually send one email per consignment, so the default is
        simply parse(). Override where a single notification can cover several
        parcels, which would otherwise silently yield only the first.
        """
        parcel = self.parse(msg)
        return [parcel] if parcel is not None else []
