"""Carrier parser registry."""

from __future__ import annotations

from email.message import Message

from ..models import Parcel
from .auspost import AusPostParser
from .base import CarrierParser
from .tge import TeamGlobalExpressParser

PARSERS: tuple[CarrierParser, ...] = (
    AusPostParser(),
    TeamGlobalExpressParser(),
)

#: Sender domains the Gmail filter should cover, derived from the parsers.
KNOWN_DOMAINS: tuple[str, ...] = tuple(
    domain for parser in PARSERS for domain in parser.from_domains
)


def parse_message(msg: Message) -> Parcel | None:
    """Run the first parser that claims this sender. Returns None if unmatched."""
    for parser in PARSERS:
        if parser.matches(msg):
            return parser.parse(msg)
    return None


__all__ = [
    "KNOWN_DOMAINS",
    "PARSERS",
    "AusPostParser",
    "CarrierParser",
    "TeamGlobalExpressParser",
    "parse_message",
]
