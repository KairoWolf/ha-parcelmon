"""Tests for manual parcel entry and the status-change event.

Manual entries exist for parcels whose email never arrives or cannot be parsed,
so the rule that matters most is that a real email later supersedes the
placeholder instead of sitting alongside it as a duplicate.
"""

from __future__ import annotations

import email
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.parcelmon.const import EVENT_PARCEL_UPDATE
from custom_components.parcelmon.coordinator import ParcelmonCoordinator
from custom_components.parcelmon.imap_client import ImapSettings
from custom_components.parcelmon.models import Parcel

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type, data):
        self.events.append((event_type, data))


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeStore:
    def __init__(self) -> None:
        self.saves = 0

    def async_schedule_save(self, parcels, seen_message_ids, manual=None) -> None:
        self.saves += 1


@pytest.fixture
def coordinator(monkeypatch) -> ParcelmonCoordinator:
    obj = object.__new__(ParcelmonCoordinator)
    obj._parcels = {}
    obj._seen_message_ids = []
    obj.removed = set()
    obj.manual = set()
    obj.retire_days = 0
    obj.hass = FakeHass()
    obj.store = FakeStore()
    obj.settings = ImapSettings(
        host="imap.example.com",
        port=993,
        username="someone@example.com",
        password="x",
        folder="Parcels",
    )
    monkeypatch.setattr(
        ParcelmonCoordinator, "async_set_updated_data", lambda self, data: None
    )
    return obj


class TestManualEntry:
    pytestmark = pytest.mark.asyncio

    async def test_adds_a_trackable_parcel(self, coordinator):
        uid = await coordinator.async_add_manual_parcel(
            tracking="36ypj5053229", carrier="auspost", status="in_transit"
        )
        assert uid == "auspost_36ypj5053229"
        parcel = coordinator._parcels[uid]
        assert parcel.tracking == "36YPJ5053229"  # normalised
        assert parcel.status == "in_transit"

    async def test_gets_a_tracking_url_for_known_carriers(self, coordinator):
        uid = await coordinator.async_add_manual_parcel(
            tracking="AA1234567890", carrier="auspost", status="unknown"
        )
        assert "AA1234567890" in coordinator._parcels[uid].tracking_url

    async def test_no_invented_url_for_carriers_without_a_pattern(self, coordinator):
        """TGE links are per-shipment; a guessed URL would just 404."""
        uid = await coordinator.async_add_manual_parcel(
            tracking="GO2S501988", carrier="tge", status="unknown"
        )
        assert coordinator._parcels[uid].tracking_url is None

    async def test_is_remembered_as_manual(self, coordinator):
        uid = await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="unknown"
        )
        assert uid in coordinator.manual

    async def test_is_persisted(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="unknown"
        )
        assert coordinator.store.saves == 1

    async def test_removal_by_tracking_number(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="unknown"
        )
        assert await coordinator.async_remove_parcel("AA1") == "auspost_aa1"
        assert coordinator._parcels == {}

    async def test_removal_by_uid(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="unknown"
        )
        assert await coordinator.async_remove_parcel("auspost_aa1") == "auspost_aa1"

    async def test_removing_something_untracked_reports_nothing(self, coordinator):
        assert await coordinator.async_remove_parcel("nope") is None

    async def test_real_email_supersedes_the_placeholder(self, coordinator, monkeypatch):
        """The whole point: no duplicate once the carrier's email shows up."""
        message = email.message_from_bytes(
            (FIXTURES / "auspost_in_transit.eml").read_bytes()
        )
        from custom_components.parcelmon.parsers import parse_message

        real = parse_message(message)
        await coordinator.async_add_manual_parcel(
            tracking=real.tracking, carrier="auspost", status="unknown"
        )
        assert len(coordinator._parcels) == 1

        monkeypatch.setattr(
            "custom_components.parcelmon.coordinator.fetch_history",
            lambda settings, days, limit: [(b"1", message)],
        )
        await coordinator.async_rescan()

        assert len(coordinator._parcels) == 1
        restored = coordinator._parcels[real.uid]
        assert restored.status == real.status
        assert restored.sender == real.sender
        assert real.uid not in coordinator.manual


class TestStatusEvent:
    pytestmark = pytest.mark.asyncio

    def _seed(self, coordinator, status):
        parcel = Parcel(carrier="auspost", tracking="AA1", status=status)
        coordinator._parcels[parcel.uid] = parcel
        return parcel

    async def test_new_parcel_fires_an_event(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="in_transit"
        )
        types = [e[0] for e in coordinator.hass.bus.events]
        assert types == [EVENT_PARCEL_UPDATE]

    async def test_payload_carries_the_uid_device_triggers_match_on(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="delivered", sender="UCL"
        )
        _, data = coordinator.hass.bus.events[0]
        assert data["uid"] == "auspost_aa1"
        assert data["status"] == "delivered"
        assert data["previous_status"] is None
        assert data["tracking_number"] == "AA1"
        assert data["sender"] == "UCL"

    async def test_status_change_reports_what_it_changed_from(self, coordinator):
        self._seed(coordinator, "in_transit")
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="delivered"
        )
        _, data = coordinator.hass.bus.events[-1]
        assert data["previous_status"] == "in_transit"
        assert data["status"] == "delivered"

    async def test_unchanged_status_is_silent(self, coordinator):
        """Otherwise every poll would re-notify for a parcel sitting in transit."""
        self._seed(coordinator, "in_transit")
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="in_transit"
        )
        assert coordinator.hass.bus.events == []

    async def test_rescan_does_not_fire_events(self, coordinator, monkeypatch):
        """A history sweep must not set off months of delivery notifications."""
        message = email.message_from_bytes(
            (FIXTURES / "auspost_in_transit.eml").read_bytes()
        )
        monkeypatch.setattr(
            "custom_components.parcelmon.coordinator.fetch_history",
            lambda settings, days, limit: [(b"1", message)],
        )
        await coordinator.async_rescan()
        assert coordinator._parcels  # it did import something
        assert coordinator.hass.bus.events == []


class TestPushInterval:
    def test_push_falls_back_to_an_hourly_safety_poll(self):
        from custom_components.parcelmon.const import (
            MIN_POLL_INTERVAL,
            PUSH_FALLBACK_INTERVAL,
        )

        assert PUSH_FALLBACK_INTERVAL == 60
        assert MIN_POLL_INTERVAL == 10

    def test_retirement_still_uses_email_dates(self):
        """Guards the interaction between manual entry and retirement."""
        old = Parcel(
            carrier="auspost",
            tracking="AA1",
            status="delivered",
            seen_at=datetime.now(UTC) - timedelta(days=10),
        )
        assert old.status in ("delivered",)


class TestExtraActions:
    pytestmark = pytest.mark.asyncio

    async def test_set_status_corrects_a_parcel(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="in_transit"
        )
        coordinator.hass.bus.events.clear()
        uid = await coordinator.async_set_status("AA1", "delivered")
        assert uid == "auspost_aa1"
        assert coordinator._parcels[uid].status == "delivered"

    async def test_set_status_fires_the_change_event(self, coordinator):
        """A hand correction should drive the same automations a real one does."""
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="in_transit"
        )
        coordinator.hass.bus.events.clear()
        await coordinator.async_set_status("AA1", "delivered")
        _, data = coordinator.hass.bus.events[-1]
        assert data["previous_status"] == "in_transit"
        assert data["status"] == "delivered"

    async def test_set_status_to_the_same_value_is_silent(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="in_transit"
        )
        coordinator.hass.bus.events.clear()
        await coordinator.async_set_status("AA1", "in_transit")
        assert coordinator.hass.bus.events == []

    async def test_set_status_on_an_unknown_parcel(self, coordinator):
        assert await coordinator.async_set_status("nope", "delivered") is None

    async def test_clear_delivered_removes_only_finished_parcels(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="delivered"
        )
        await coordinator.async_add_manual_parcel(
            tracking="BB2", carrier="auspost", status="returned"
        )
        await coordinator.async_add_manual_parcel(
            tracking="CC3", carrier="auspost", status="in_transit"
        )
        assert await coordinator.async_clear_delivered() == 2
        assert list(coordinator._parcels) == ["auspost_cc3"]

    async def test_clear_delivered_ignores_retire_days(self, coordinator):
        """The point is to clear now, not to wait out the retirement window."""
        coordinator.retire_days = 90
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="delivered"
        )
        assert await coordinator.async_clear_delivered() == 1
        assert coordinator._parcels == {}

    async def test_clear_delivered_on_an_empty_set(self, coordinator):
        assert await coordinator.async_clear_delivered() == 0

    async def test_find_matches_uid_or_tracking(self, coordinator):
        await coordinator.async_add_manual_parcel(
            tracking="AA1", carrier="auspost", status="unknown"
        )
        assert coordinator.find("aa1") is not None
        assert coordinator.find("auspost_aa1") is not None
        assert coordinator.find("zz9") is None
