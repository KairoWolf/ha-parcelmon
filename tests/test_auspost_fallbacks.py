"""Tests for the AusPost tracking-number fallbacks.

The strict `Tracking number: XXXX` pattern is the primary path and is exercised
against the real fixture in test_parsers.py. These cover the cases that made a
parcel silently vanish instead: a different label, a number split by markup, and
one email carrying two consignments.

The emails here are synthesised, not real carrier captures. They exist to pin
the fallback behaviour; the real templates remain the fixtures under fixtures/.
"""

from __future__ import annotations

import email
import pathlib

from custom_components.parcelmon.parsers import parse_message, parse_message_all
from custom_components.parcelmon.parsers.auspost import _candidates

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def build(body: str, subject: str = "Your parcel is on its way"):
    raw = (
        'From: "Australia Post" <noreply@notifications.auspost.com.au>\n'
        "To: <someone@example.com>\n"
        f"Subject: {subject}\n"
        "Date: Sun, 26 Jul 2026 15:44:27 -0600\n"
        "Message-ID: <synthetic@notifications.auspost.com.au>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/html; charset="utf-8"\n\n'
        f"<html><body>{body}</body></html>"
    )
    return email.message_from_string(raw)


class TestPrimaryPathUnchanged:
    def test_real_fixture_yields_exactly_one_parcel(self):
        """The regression that matters: don't invent parcels in mail that works."""
        msg = email.message_from_bytes(
            (FIXTURES / "auspost_in_transit.eml").read_bytes()
        )
        parcels = parse_message_all(msg)
        assert len(parcels) == 1
        assert parcels[0].tracking == "36YPJ5053229"

    def test_prose_after_the_number_is_not_absorbed(self):
        msg = build("<p>Tracking number: 36YPJ5053229 Delivering to NSW 2429</p>")
        assert _candidates(*_text_and_html(msg)) == ["36YPJ5053229"]


def _text_and_html(msg):
    from custom_components.parcelmon.parsers.base import (
        html_part,
        soup_of,
        visible_text,
    )

    return visible_text(soup_of(msg)), html_part(msg)


class TestAlternativeLabels:
    def test_consignment_number(self):
        msg = build("<p>Consignment number: 7XYZ4455661</p>")
        assert parse_message(msg).tracking == "7XYZ4455661"

    def test_article_id(self):
        msg = build("<p>Article ID 33ABCD990011</p>")
        assert parse_message(msg).tracking == "33ABCD990011"

    def test_tracking_no_abbreviated(self):
        msg = build("<p>Tracking no. 36YPJ5053229</p>")
        assert parse_message(msg).tracking == "36YPJ5053229"

    def test_label_without_a_number_is_not_a_parcel(self):
        """'Tracking number unavailable' must not become a consignment id."""
        msg = build("<p>Tracking number unavailable at this time</p>")
        assert parse_message(msg) is None


class TestSplitByMarkup:
    def test_number_split_across_spans(self):
        # visible_text() joins adjacent tags with a space, so per-chunk styling
        # arrives as "36YPJ 5053229".
        msg = build(
            "<p>Tracking number: <span>36YPJ</span><span>5053229</span></p>"
        )
        assert parse_message(msg).tracking == "36YPJ5053229"

    def test_number_split_per_character(self):
        chars = "".join(f"<b>{c}</b>" for c in "36YPJ5053229")
        msg = build(f"<p>Tracking number: {chars}</p>")
        assert parse_message(msg).tracking == "36YPJ5053229"

    def test_split_path_does_not_run_when_a_number_was_already_found(self):
        """It is guesswork, so it must never compete with a clean match."""
        msg = build("<p>Tracking number: 36YPJ5053229 EXPECTED BY FRIDAY</p>")
        assert [p.tracking for p in parse_message_all(msg)] == ["36YPJ5053229"]


class TestMultipleConsignments:
    def test_two_parcels_in_one_email(self):
        """The failure mode that loses a parcel with no warning at all."""
        msg = build(
            "<h1>Your parcels are on their way</h1>"
            "<p>Tracking number: 36YPJ5053229</p>"
            "<p>Tracking number: 7XYZ4455661</p>"
        )
        parcels = parse_message_all(msg)
        assert [p.tracking for p in parcels] == ["36YPJ5053229", "7XYZ4455661"]

    def test_each_gets_its_own_uid_and_url(self):
        msg = build(
            "<p>Tracking number: 36YPJ5053229</p><p>Tracking number: 7XYZ4455661</p>"
        )
        parcels = parse_message_all(msg)
        assert len({p.uid for p in parcels}) == 2
        assert parcels[1].tracking in parcels[1].tracking_url

    def test_only_the_first_claims_the_message_id(self):
        """Sharing it would make the coordinator drop the second as a duplicate."""
        msg = build(
            "<p>Tracking number: 36YPJ5053229</p><p>Tracking number: 7XYZ4455661</p>"
        )
        parcels = parse_message_all(msg)
        assert parcels[0].message_id is not None
        assert parcels[1].message_id is None

    def test_shared_details_are_applied_to_both(self):
        msg = build(
            "<h1>On its way</h1>"
            "<p>From <strong>UCL Co. Ltd</strong> Tracking number: 36YPJ5053229</p>"
            "<p>Tracking number: 7XYZ4455661</p>"
            "<p>Delivering to: NSW 2429</p>"
        )
        parcels = parse_message_all(msg)
        assert all(p.destination == "NSW 2429" for p in parcels)
        assert all(p.status == "in_transit" for p in parcels)

    def test_repeated_number_is_not_duplicated(self):
        msg = build(
            "<p>Tracking number: 36YPJ5053229</p>"
            "<p>Tracking number: 36YPJ5053229</p>"
        )
        assert len(parse_message_all(msg)) == 1


class TestGuards:
    def test_a_candidate_needs_a_digit(self):
        msg = build("<p>Consignment number: ABCDEFGHIJ</p>")
        assert parse_message(msg) is None

    def test_too_short_is_rejected(self):
        msg = build("<p>Tracking number: 36YPJ5</p>")
        assert parse_message(msg) is None

    def test_unmatched_sender_still_ignored(self):
        msg = build("<p>Tracking number: 36YPJ5053229</p>")
        msg.replace_header("From", "newsletter@example.com")
        assert parse_message_all(msg) == []
