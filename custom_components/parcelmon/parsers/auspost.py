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
from .base import CarrierParser, header_date, soup_of, squash, visible_text

# AusPost consignment numbers are alphanumeric, typically 10-24 chars.
RE_TRACKING = re.compile(r"Tracking number:?\s*([A-Z0-9]{8,24})\b", re.I)
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
        soup = soup_of(msg)
        if soup is None:
            return None

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

        m = RE_TRACKING.search(text)
        if not m:
            # No consignment number means nothing we can key an entity on.
            return None
        tracking = m.group(1).upper()

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

        return Parcel(
            carrier=self.carrier,
            tracking=tracking,
            status=classify_prioritised(headline, subject, text),
            status_text=headline or subject,
            sender=sender,
            eta=eta,
            destination=destination,
            delivered_on=delivered_on,
            tracking_url=TRACK_URL.format(tracking),
            photo=None,  # never present in AusPost mail
            photo_url=None,
            subject=subject,
            message_id=squash(msg.get("Message-ID")) or None,
            email_date=header_date(msg),
        )
