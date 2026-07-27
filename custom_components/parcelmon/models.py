"""Shared data model for parsed parcel notifications."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Normalised status vocabulary. Everything a parser emits must be one of these.
IN_TRANSIT = "in_transit"
OUT_FOR_DELIVERY = "out_for_delivery"
DELIVERED = "delivered"
ATTEMPTED = "attempted"
AWAITING_COLLECTION = "awaiting_collection"
RETURNED = "returned"
UNKNOWN = "unknown"

STATUSES = (
    IN_TRANSIT,
    OUT_FOR_DELIVERY,
    DELIVERED,
    ATTEMPTED,
    AWAITING_COLLECTION,
    RETURNED,
    UNKNOWN,
)

# Ordered most-specific first: the first pattern to match wins.
_STATUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (DELIVERED, r"\b(has been delivered|was delivered|delivered|left in a safe place|safe ?drop(ped)?)\b"),
    (ATTEMPTED, r"\b(we missed you|missed delivery|attempted delivery|couldn'?t deliver|unable to deliver|nobody (was )?home)\b"),
    (AWAITING_COLLECTION, r"\b(ready (for|to) collect(ion)?|ready for pick ?up|awaiting collection|at the post office|collect it from)\b"),
    (RETURNED, r"\b(returned to sender|being returned|return to sender)\b"),
    (OUT_FOR_DELIVERY, r"\b(out for delivery|on board for delivery|on the van|with the driver|arriving today)\b"),
    (IN_TRANSIT, r"\b(on its way|in transit|we'?ve got it|has been sent|is coming|dispatched|shipped)\b"),
)


def classify(*texts: str | None) -> str:
    """Map free text (headline, subject, body) onto the normalised vocabulary."""
    blob = " ".join(t for t in texts if t).lower()
    blob = blob.replace("\u00a0", " ")
    for status, pattern in _STATUS_PATTERNS:
        if re.search(pattern, blob):
            return status
    return UNKNOWN


def classify_prioritised(primary: str | None, secondary: str | None = None,
                         fallback: str | None = None) -> str:
    """Classify against the headline first, only consulting the body if needed.

    Necessary because carrier boilerplate lies. An AusPost *in transit* email
    carries the footer "Safe Drop is only available for locations not in public
    view", and classifying the whole body on that would report the parcel as
    delivered. The h1 and subject are the only trustworthy signals.
    """
    for candidate in (primary, secondary, fallback):
        status = classify(candidate)
        if status != UNKNOWN:
            return status
    return UNKNOWN


def slug(value: str) -> str:
    """Safe fragment for MQTT topics and HA object_ids."""
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


@dataclass
class Parcel:
    """One parcel state update, extracted from one email."""

    carrier: str
    tracking: str
    status: str = UNKNOWN
    status_text: str | None = None
    sender: str | None = None
    eta: str | None = None
    destination: str | None = None
    delivered_on: str | None = None
    tracking_url: str | None = None
    photo: bytes | None = field(default=None, repr=False)
    photo_url: str | None = None
    subject: str | None = None
    message_id: str | None = None
    email_date: datetime | None = None
    seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def uid(self) -> str:
        """Stable identifier used for MQTT topics and HA unique_ids."""
        return f"{slug(self.carrier)}_{slug(self.tracking)}"

    @property
    def has_photo(self) -> bool:
        return bool(self.photo)

    def attributes(self) -> dict:
        """Everything except the photo bytes, for the MQTT attributes topic."""
        return {
            "carrier": self.carrier,
            "tracking_number": self.tracking,
            "status": self.status,
            "status_text": self.status_text,
            "sender": self.sender,
            "eta": self.eta,
            "destination": self.destination,
            "delivered_on": self.delivered_on,
            "tracking_url": self.tracking_url,
            "has_photo": self.has_photo,
            "photo_url": self.photo_url,
            "subject": self.subject,
            "email_date": self.email_date.isoformat() if self.email_date else None,
            "last_seen": self.seen_at.isoformat(),
        }
