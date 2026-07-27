"""Australia Post (noreply@notifications.auspost.com.au).

AusPost send a single-part text/html body built in Salesforce Marketing Cloud.
The layout is deep nested tables, but the *text* is stable and label-driven:

    It's on its way
    From <strong>UCL Co. Ltd</strong>
    Tracking number: <strong>36YPJ5053229</strong>
    Delivering to: <strong>NSW 2429</strong>
    Expected delivery: <strong>Wednesday 29 Jul 2026 - Thursday 30 Jul 2026*</strong>

So we parse against the rendered text rather than CSS selectors, which survives
their table markup being reshuffled.

Note: AusPost do NOT embed the Safe Drop photo in the email. The footer says it
"may be available in the app" - it is behind MyPost auth only. photo stays None.
"""

from __future__ import annotations

import re
from email.message import Message

from ..models import Parcel, classify_prioritised
from .base import (
    CarrierParser,
    header_date,
    html_part,
    soup_of,
    squash,
    visible_text,
)

# AusPost consignment numbers are alphanumeric, typically 10-24 chars.
RE_TRACKING = re.compile(r"Tracking number:?\s*([A-Z0-9]{8,24})\b", re.I)

# Fallbacks, tried only when the line above finds nothing. Two things go wrong
# in the wild: AusPost do not always use the words "Tracking number" (older and
# transactional templates say consignment/article/shipment), and the number can
# arrive split by markup, because visible_text() joins adjacent tags with a
# space and their number is sometimes wrapped per-character in styled spans.
# Hence the tolerated internal whitespace, squashed out before validating.
RE_TRACKING_LABELLED = re.compile(
    r"\b(?:tracking|consignment|article|shipment)\s*"
    r"(?:numbers?|no\.?|id)?\s*[:#]?\s*"
    r"([A-Z0-9]{8,24})\b",
    re.I,
)
# Last resort, and only when nothing else matched at all: the number arrived
# split into chunks. Case-sensitive on purpose - a consignment number is
# upper-case, so requiring that stops the run from eating the sentence after it
# ("36YPJ5053229 Delivering to ..."), and the lookahead stops it mid-word.
RE_TRACKING_SPLIT = re.compile(
    r"(?i:\b(?:tracking|consignment|article|shipment)\s*"
    r"(?:numbers?|no\.?|id)?\s*[:#]?\s*)"
    r"((?:[A-Z0-9]{1,12}[ \t]){1,11}[A-Z0-9]{1,12})(?![a-z0-9])"
)
# Direct deep links carry the number; the marketing sends use Salesforce click
# redirects (auspost.com.au/u/?qs=...) which do not, so this only sometimes helps.
RE_TRACKING_URL = re.compile(
    r"auspost\.com\.au/(?:mypost/)?track(?:/details)?/([A-Z0-9]{8,24})\b", re.I
)
# A consignment number always carries at least one digit. Without this, prose
# following a label ("Tracking number unavailable") would be taken as an id.
RE_HAS_DIGIT = re.compile(r"\d")


def _candidates(text: str, html: str) -> list[str]:
    """Every plausible consignment number, best-guess first, de-duplicated.

    Ordered so the long-standing exact pattern always wins: the loosened ones
    only contribute numbers it did not already find.
    """
    found: list[str] = []

    def _add(raw: str) -> None:
        value = re.sub(r"[\s]+", "", raw).upper()
        if not (8 <= len(value) <= 24):
            return
        if not RE_HAS_DIGIT.search(value):
            return
        if value not in found:
            found.append(value)

    for match in RE_TRACKING.finditer(text):
        _add(match.group(1))
    for match in RE_TRACKING_LABELLED.finditer(text):
        _add(match.group(1))
    for match in RE_TRACKING_URL.finditer(html):
        _add(match.group(1))
    if found:
        # Only reach for the loose pattern when the strict ones came up empty.
        # Reassembling a split number is guesswork, and guessing next to a
        # number we already read correctly is how you invent a second parcel.
        return found

    for match in RE_TRACKING_SPLIT.finditer(text):
        _add(match.group(1))
    return found
RE_SENDER_BODY = re.compile(r"\bFrom\s+(.{1,80}?)\s+Tracking number", re.I)
RE_SENDER_SUBJ = re.compile(r"parcel from\s+(.+?)\s+(?:is|has|was|will|are)\b", re.I)
RE_DESTINATION = re.compile(r"Delivering to:?\s*([A-Z]{2,3}\s*\d{4})\b")
RE_ETA = re.compile(
    r"Expected delivery:?\s*(.{3,80}?)\s*(?:\*|Delivery options|Track your|$)", re.I
)
RE_PREHEADER_ETA = re.compile(r"Expected delivery by\s+(.{3,60}?)\s*\.", re.I)
RE_DELIVERED_ON = re.compile(
    r"delivered (?:on|at)\s+((?:[A-Z][a-z]+day,?\s*)?\d{1,2}\s+[A-Z][a-z]+(?:\s+\d{4})?)", re.I
)

TRACK_URL = "https://auspost.com.au/mypost/track/details/{}"


class AusPostParser(CarrierParser):
    carrier = "auspost"
    from_domains = ("auspost.com.au",)

    def parse(self, msg: Message) -> Parcel | None:
        """The first parcel in the email, or None."""
        parcels = self.parse_all(msg)
        return parcels[0] if parcels else None

    def parse_all(self, msg: Message) -> list[Parcel]:
        """Every consignment in the email.

        AusPost sometimes cover more than one parcel in a single notification.
        Taking only the first match silently loses the rest, which looks
        identical to the parcel never having been emailed about at all.
        """
        soup = soup_of(msg)
        if soup is None:
            return []

        # The preheader is display:none but still lands in get_text(); pull it
        # out first so it can serve as an ETA fallback without polluting the body.
        preheader = ""
        for node in soup.select(".preheader"):
            preheader += " " + squash(node.get_text(" "))
            node.decompose()

        headline = None
        h1 = soup.find("h1")
        if h1:
            headline = squash(h1.get_text(" ")) or None

        text = visible_text(soup)
        subject = squash(msg.get("Subject")) or None

        trackings = _candidates(text, html_part(msg))
        if not trackings:
            # No consignment number means nothing we can key an entity on.
            return []

        sender = None
        if (sm := RE_SENDER_BODY.search(text)) or (subject and (sm := RE_SENDER_SUBJ.search(subject))):
            sender = squash(sm.group(1))
        if sender and sender.lower() in ("", "us", "the sender"):
            sender = None

        eta = None
        if em := RE_ETA.search(text):
            eta = squash(em.group(1)).rstrip("*").strip() or None
        if not eta and (pm := RE_PREHEADER_ETA.search(preheader)):
            eta = squash(pm.group(1)) or None

        destination = None
        if dm := RE_DESTINATION.search(text):
            destination = squash(dm.group(1))

        delivered_on = None
        if dm := RE_DELIVERED_ON.search(text):
            delivered_on = squash(dm.group(1))

        status = classify_prioritised(headline, subject, text)
        message_id = squash(msg.get("Message-ID")) or None
        email_date = header_date(msg)

        # Sender, ETA and destination are stated once for the whole email, so a
        # multi-consignment notification shares them across its parcels.
        return [
            Parcel(
                carrier=self.carrier,
                tracking=tracking,
                status=status,
                status_text=headline or subject,
                sender=sender,
                eta=eta,
                destination=destination,
                delivered_on=delivered_on,
                tracking_url=TRACK_URL.format(tracking),
                photo=None,  # never present in AusPost mail
                photo_url=None,
                subject=subject,
                # Only the first parcel claims the Message-ID: it is the
                # de-duplication key, and sharing it would make the coordinator
                # treat the second parcel as an already-handled duplicate.
                message_id=message_id if index == 0 else None,
                email_date=email_date,
            )
            for index, tracking in enumerate(trackings)
        ]
