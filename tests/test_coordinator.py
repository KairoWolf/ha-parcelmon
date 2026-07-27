"""Tests for coordinator state-merging rules (no Home Assistant runtime needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.parcelmon.coordinator import ParcelmonCoordinator
from custom_components.parcelmon.models import Parcel


@pytest.fixture
def coordinator() -> ParcelmonCoordinator:
    """A coordinator with only the fields the merge/retire logic touches."""
    obj = object.__new__(ParcelmonCoordinator)
    obj._parcels = {}
    obj._seen_message_ids = []
    obj.removed = set()
    obj.retire_days = 3
    return obj


def make(tracking="AA1", **kw) -> Parcel:
    return Parcel(carrier="auspost", tracking=tracking, **kw)


class TestMerge:
    def test_new_parcel_is_stored(self, coordinator):
        coordinator._merge(make(status="in_transit"))
        assert coordinator._parcels["auspost_aa1"].status == "in_transit"

    def test_newer_status_wins(self, coordinator):
        coordinator._merge(make(status="in_transit"))
        coordinator._merge(make(status="delivered"))
        assert coordinator._parcels["auspost_aa1"].status == "delivered"

    def test_later_email_does_not_erase_sender(self, coordinator):
        # AusPost drop the merchant name from follow-up emails. Without the
        # merge rule, a delivered notification would blank the sender you
        # actually want to put in the push notification.
        coordinator._merge(make(status="in_transit", sender="UCL Co. Ltd", eta="Wed 29 Jul"))
        coordinator._merge(make(status="delivered", sender=None, eta=None))
        merged = coordinator._parcels["auspost_aa1"]
        assert merged.status == "delivered"
        assert merged.sender == "UCL Co. Ltd"
        assert merged.eta == "Wed 29 Jul"

    def test_newer_value_still_overrides_when_present(self, coordinator):
        coordinator._merge(make(sender="Old Merchant"))
        coordinator._merge(make(sender="New Merchant"))
        assert coordinator._parcels["auspost_aa1"].sender == "New Merchant"

    def test_photo_survives_a_later_photoless_email(self, coordinator):
        coordinator._merge(make(status="delivered", photo=b"\xff\xd8\xffJPEG"))
        coordinator._merge(make(status="delivered", photo=None))
        assert coordinator._parcels["auspost_aa1"].has_photo

    def test_distinct_tracking_numbers_do_not_collide(self, coordinator):
        coordinator._merge(make("AA1"))
        coordinator._merge(make("BB2"))
        assert len(coordinator._parcels) == 2


class TestRetire:
    def _aged(self, days: int, status: str) -> Parcel:
        parcel = make(status=status)
        parcel.seen_at = datetime.now(UTC) - timedelta(days=days)
        return parcel

    def test_old_delivered_parcel_is_removed(self, coordinator):
        coordinator._merge(self._aged(5, "delivered"))
        coordinator._retire_stale()
        assert coordinator._parcels == {}
        assert "auspost_aa1" in coordinator.removed

    def test_recent_delivered_parcel_is_kept(self, coordinator):
        coordinator._merge(self._aged(1, "delivered"))
        coordinator._retire_stale()
        assert "auspost_aa1" in coordinator._parcels

    def test_old_in_transit_parcel_is_kept(self, coordinator):
        # A parcel stuck in transit for a month is exactly what you want to see.
        coordinator._merge(self._aged(30, "in_transit"))
        coordinator._retire_stale()
        assert "auspost_aa1" in coordinator._parcels

    def test_zero_days_disables_retirement(self, coordinator):
        coordinator.retire_days = 0
        coordinator._merge(self._aged(365, "delivered"))
        coordinator._retire_stale()
        assert "auspost_aa1" in coordinator._parcels
