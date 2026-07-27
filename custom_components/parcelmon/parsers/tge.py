"""Team Global Express (myteamge@teamglobalexp.com), formerly Toll.

Sent via Amazon SES as multipart/mixed with a quoted-printable text/HTML part.
Two very useful anchors that AusPost doesn't give us:

1. The "View Details" button href carries structured state:
       .../myparcel?shipmentID=GO2S501988&...&status=delivered
   The status query parameter is authoritative - no headline guessing needed.

2. The proof-of-delivery photo is embedded as a base64 data URI in the img
   srcset, with a p.mytge.co shortlink in src. We take the base64, because the
   shortlink expires (their footer says the photo lives for 7 days).
"""

from __future__ import annotations

import base64
import binascii
import re
from email.message import Message
from urllib.parse import parse_qs, urlparse

from ..models import Parcel, classify_prioritised
from .base import CarrierParser, header_date, soup_of, squash, visible_text

RE_TRACKING_TEXT = re.compile(r"\bparcel\s+([A-Z]{2}[A-Z0-9]{4,22})\b")
RE_SENDER = re.compile(r"parcel from\s+(.+?)\s+(?:has been|is|was|will)\b", re.I)
RE_YOUR_SENDER = re.compile(r"\bYour\s+(.{1,60}?)\s+parcel\s+[A-Z0-9]{6,}", re.I)
RE_DELIVERED_ON = re.compile(
    r"delivered on\s+((?:[A-Z][a-z]+day\s+)?\d{1,2}\s+[A-Z][a-z]+(?:\s+\d{4})?)", re.I
)
RE_ETA = re.compile(r"(?:estimated|expected) delivery(?:\s+date)?:?\s*(.{3,60}?)\s*(?:\.|$)", re.I)
RE_DATA_URI = re.compile(r"data:image/(?P<fmt>jpeg|jpg|png|gif|webp);base64,(?P<b64>[A-Za-z0-9+/=\s]+)")

# status= values seen in their myparcel links, mapped onto our vocabulary.
STATUS_PARAM = {
    "delivered": "delivered",
    "intransit": "in_transit",
    "in_transit": "in_transit",
    "transit": "in_transit",
    "outfordelivery": "out_for_delivery",
    "out_for_delivery": "out_for_delivery",
    "attempted": "attempted",
    "carded": "attempted",
    "awaitingcollection": "awaiting_collection",
    "returned": "returned",
}


def _decode_data_uri(value: str) -> bytes | None:
    """Pull image bytes out of a data: URI, tolerating whitespace from QP wrapping."""
    m = RE_DATA_URI.search(value or "")
    if not m:
        return None
    payload = re.sub(r"\s+", "", m.group("b64"))
    # Base64 in email is routinely truncated or re-wrapped; pad rather than fail.
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    return raw or None


class TeamGlobalExpressParser(CarrierParser):
    carrier = "tge"
    from_domains = ("teamglobalexp.com", "mytge.com", "tollgroup.com")

    def parse(self, msg: Message) -> Parcel | None:
        soup = soup_of(msg)
        if soup is None:
            return None

        subject = squash(msg.get("Subject")) or None

        # 1. The myparcel link: shipment id and status in one place.
        tracking = None
        status = None
        tracking_url = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "myparcel" not in href.lower():
                continue
            qs = parse_qs(urlparse(href).query)
            sid = (qs.get("shipmentID") or qs.get("shipmentid") or [None])[0]
            if not sid:
                continue
            tracking = sid.strip().upper()
            tracking_url = href
            raw_status = (qs.get("status") or [""])[0].strip().lower().replace("-", "")
            status = STATUS_PARAM.get(raw_status)
            break

        headline = None
        if subj_div := soup.find(id="subject"):
            headline = squash(subj_div.get_text(" ")) or None

        text = visible_text(soup)

        # 2. Fall back to the body if the link was missing or rewritten.
        if not tracking and (m := RE_TRACKING_TEXT.search(text)):
            tracking = m.group(1).upper()
        if not tracking:
            return None
        if not status:
            status = classify_prioritised(headline, subject, text)

        sender = None
        for pattern, source in ((RE_SENDER, headline or subject or ""), (RE_YOUR_SENDER, text)):
            if sm := pattern.search(source):
                sender = squash(sm.group(1))
                break

        delivered_on = None
        if dm := RE_DELIVERED_ON.search(text):
            delivered_on = squash(dm.group(1))

        eta = None
        if em := RE_ETA.search(text):
            eta = squash(em.group(1)) or None

        # 3. Proof-of-delivery photo. Prefer embedded bytes over the shortlink.
        photo = None
        photo_url = None
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").lower()
            candidates = [img.get("srcset") or "", img.get("src") or ""]
            if "pod" not in alt and not any("data:image" in c for c in candidates):
                continue
            for candidate in candidates:
                if photo is None:
                    photo = _decode_data_uri(candidate)
                if photo_url is None and candidate.startswith(("http://", "https://")):
                    photo_url = candidate
            if photo or photo_url:
                break

        return Parcel(
            carrier=self.carrier,
            tracking=tracking,
            status=status,
            status_text=headline or subject,
            sender=sender,
            eta=eta,
            destination=None,
            delivered_on=delivered_on,
            tracking_url=tracking_url,
            photo=photo,
            photo_url=photo_url,
            subject=subject,
            message_id=squash(msg.get("Message-ID")) or None,
            email_date=header_date(msg),
        )
