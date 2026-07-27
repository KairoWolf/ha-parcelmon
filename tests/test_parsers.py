"""Parser regression tests, run against fixtures derived from real emails."""

from __future__ import annotations

import email
import pathlib

import pytest

from custom_components.parcelmon.models import (
    DELIVERED,
    IN_TRANSIT,
    UNKNOWN,
    classify,
    classify_prioritised,
)
from custom_components.parcelmon.parsers import parse_message

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return email.message_from_file(fh)


@pytest.fixture(scope="module")
def auspost():
    return parse_message(load("auspost_in_transit.eml"))


@pytest.fixture(scope="module")
def tge():
    return parse_message(load("tge_delivered.eml"))


class TestAusPost:
    def test_parsed(self, auspost):
        assert auspost is not None
        assert auspost.carrier == "auspost"

    def test_tracking_number(self, auspost):
        assert auspost.tracking == "36YPJ5053229"

    def test_status_is_in_transit_not_delivered(self, auspost):
        # Regression: the footer boilerplate "Safe Drop is only available..."
        # used to classify an in-transit parcel as delivered.
        assert auspost.status == IN_TRANSIT

    def test_headline_and_sender(self, auspost):
        assert auspost.status_text == "It's on its way"
        assert auspost.sender == "UCL Co. Ltd"

    def test_eta_strips_asterisk(self, auspost):
        assert auspost.eta is not None
        assert auspost.eta.startswith("Wednesday 29 Jul 2026")
        assert not auspost.eta.endswith("*")

    def test_destination(self, auspost):
        assert auspost.destination == "NSW 2429"

    def test_canonical_tracking_url(self, auspost):
        assert auspost.tracking_url.endswith("/36YPJ5053229")

    def test_no_photo(self, auspost):
        # AusPost never embed the Safe Drop image; it is MyPost-app only.
        assert auspost.photo is None
        assert auspost.has_photo is False

    def test_preheader_excluded_from_eta(self, auspost):
        # The hidden preheader says "Expected delivery by Thursday 30 Jul 2026."
        # The visible row is the range, and the range should win.
        assert "\u2013" in auspost.eta or "-" in auspost.eta


class TestTeamGlobalExpress:
    def test_parsed(self, tge):
        assert tge is not None
        assert tge.carrier == "tge"

    def test_shipment_id_from_link(self, tge):
        assert tge.tracking == "GO2S501988"

    def test_status_from_query_param(self, tge):
        assert tge.status == DELIVERED

    def test_sender_and_date(self, tge):
        assert tge.sender == "UBI Logistics"
        assert tge.delivered_on == "Thursday 23 July"

    def test_photo_decoded_from_base64_srcset(self, tge):
        assert tge.has_photo
        assert tge.photo.startswith(b"\xff\xd8\xff"), "expected JPEG magic bytes"
        assert len(tge.photo) > 100

    def test_photo_shortlink_kept_as_url(self, tge):
        assert tge.photo_url == "http://p.mytge.co/N1QU2M4jP3"

    def test_quoted_printable_survived(self, tge):
        # If QP decoding failed we would see '=3D' artefacts in the link.
        assert "=3D" not in tge.tracking_url

    def test_defanged_attributes_tolerated(self, tge):
        # Their mail arrives with defang_contenteditable= rewritten attributes.
        assert tge.status_text.startswith("Your parcel from UBI Logistics")


class TestRouting:
    def test_unknown_sender_ignored(self):
        msg = email.message_from_string(
            "From: spam@example.com\nSubject: Your parcel is here\n\nhello"
        )
        assert parse_message(msg) is None

    def test_uid_is_topic_safe(self, tge, auspost):
        for parcel in (tge, auspost):
            assert parcel.uid.replace("_", "").isalnum()


class TestClassifier:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("It's on its way", IN_TRANSIT),
            ("Your parcel has been delivered", DELIVERED),
            ("We missed you", "attempted"),
            ("Your parcel is ready for collection", "awaiting_collection"),
            ("On board for delivery", "out_for_delivery"),
            ("Returned to sender", "returned"),
            ("Quarterly newsletter", UNKNOWN),
        ],
    )
    def test_vocabulary(self, text, expected):
        assert classify(text) == expected

    def test_headline_beats_body_boilerplate(self):
        assert classify_prioritised(
            "It's on its way",
            None,
            "Safe Drop is only available for locations not in public view",
        ) == IN_TRANSIT

    def test_falls_through_to_body_when_headline_silent(self):
        assert classify_prioritised(
            "Australia Post", None, "your parcel has been delivered"
        ) == DELIVERED
