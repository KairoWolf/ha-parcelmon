"""Tests for parcel persistence.

Mail is marked read once parsed, so a parcel lost at shutdown is lost for good:
the message never reappears in an UNSEEN search. These cover the serialisation
round-trip and the tolerance rules that stop a stale or corrupt file from
breaking setup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.parcelmon.coordinator import ParcelmonCoordinator
from custom_components.parcelmon.models import Parcel
from custom_components.parcelmon.store import parcel_from_dict, parcel_to_dict


def make(**kw) -> Parcel:
    base = {
        "carrier": "tge",
        "tracking": "GO2S501988",
        "status": "in_transit",
        "sender": "UBI Logistics",
        "eta": "Thu 30 Jul",
        "destination": "Fitzroy VIC",
        "tracking_url": "https://example.com/track",
        "subject": "Your parcel is on its way",
        "message_id": "<abc@example.com>",
        "email_date": datetime(2026, 7, 23, 0, 37, 40, tzinfo=UTC),
        "seen_at": datetime(2026, 7, 23, 1, 0, 0, tzinfo=UTC),
    }
    return Parcel(**(base | kw))


class TestRoundTrip:
    def test_every_field_survives(self):
        original = make()
        restored = parcel_from_dict(parcel_to_dict(original))
        assert restored == original

    def test_photo_bytes_survive(self):
        # JSON cannot hold bytes; the photo is base64 on the way out.
        original = make(photo=b"\x89PNG\r\n\x1a\n\xde\xad\xbe\xef")
        restored = parcel_from_dict(parcel_to_dict(original))
        assert restored.photo == original.photo
        assert restored.has_photo

    def test_datetimes_keep_their_timezone(self):
        restored = parcel_from_dict(parcel_to_dict(make()))
        assert restored.seen_at.tzinfo is not None
        assert restored.email_date == datetime(2026, 7, 23, 0, 37, 40, tzinfo=UTC)

    def test_uid_is_stable_across_a_restart(self):
        original = make()
        assert parcel_from_dict(parcel_to_dict(original)).uid == original.uid

    def test_json_serialisable(self):
        import json

        json.dumps(parcel_to_dict(make(photo=b"\x00\x01\x02")))


class TestTolerance:
    def test_unknown_keys_are_ignored(self):
        """A file written by a newer version must not break setup."""
        data = parcel_to_dict(make()) | {"invented_field": "boom"}
        assert parcel_from_dict(data).tracking == "GO2S501988"

    def test_missing_optional_fields_fall_back_to_defaults(self):
        data = {"carrier": "auspost", "tracking": "AA1"}
        parcel = parcel_from_dict(data)
        assert parcel.status == "unknown"
        assert parcel.photo is None
        assert parcel.seen_at.tzinfo is not None

    def test_record_without_tracking_is_discarded(self):
        assert parcel_from_dict({"carrier": "auspost"}) is None

    def test_unparseable_date_is_dropped_not_fatal(self):
        data = parcel_to_dict(make()) | {"email_date": "not-a-date"}
        parcel = parcel_from_dict(data)
        assert parcel is not None
        assert parcel.email_date is None


class FakeStore:
    """Stands in for ParcelmonStore without touching the filesystem."""

    def __init__(self, parcels=None, seen=None):
        self._parcels = parcels or {}
        self._seen = seen or []
        self.saves = 0

    async def async_load(self):
        return dict(self._parcels), list(self._seen)

    def async_schedule_save(self, parcels, seen_message_ids):
        self.saves += 1


@pytest.fixture
def coordinator() -> ParcelmonCoordinator:
    obj = object.__new__(ParcelmonCoordinator)
    obj._parcels = {}
    obj._seen_message_ids = []
    obj.removed = set()
    obj.retire_days = 3
    return obj


class TestRestore:
    # pytest-asyncio runs in strict mode here, so the coroutine tests need marking.
    pytestmark = pytest.mark.asyncio

    async def test_parcels_come_back_after_a_restart(self, coordinator):
        parcel = make(seen_at=datetime.now(UTC))
        coordinator.store = FakeStore({parcel.uid: parcel}, ["<abc@example.com>"])
        await coordinator.async_restore()
        assert coordinator._parcels[parcel.uid].tracking == "GO2S501988"
        assert coordinator._seen_message_ids == ["<abc@example.com>"]

    async def test_handled_message_ids_survive(self, coordinator):
        """Otherwise a restart would re-import and re-notify every parcel."""
        coordinator.store = FakeStore({}, ["<one@x>", "<two@x>"])
        await coordinator.async_restore()
        assert coordinator._seen_message_ids == ["<one@x>", "<two@x>"]

    async def test_parcels_that_finished_during_downtime_are_retired(
        self, coordinator
    ):
        """A week-old delivery should not reappear just because it was on disk."""
        stale = make(status="delivered", seen_at=datetime.now(UTC) - timedelta(days=7))
        coordinator.store = FakeStore({stale.uid: stale})
        await coordinator.async_restore()
        assert coordinator._parcels == {}

    async def test_in_transit_parcel_survives_a_long_outage(self, coordinator):
        """Only finished parcels retire; an in-flight one is still wanted."""
        old = make(status="in_transit", seen_at=datetime.now(UTC) - timedelta(days=30))
        coordinator.store = FakeStore({old.uid: old})
        await coordinator.async_restore()
        assert old.uid in coordinator._parcels

    async def test_empty_store_is_not_an_error(self, coordinator):
        coordinator.store = FakeStore()
        await coordinator.async_restore()
        assert coordinator._parcels == {}
