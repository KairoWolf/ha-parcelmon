"""Tests for the rescan action, which backfills parcels from already-read mail.

Routine polling searches UNSEEN only, so mail read before Parcelmon was
installed is invisible to it. These cover the rules that make a rescan safe to
run repeatedly: it must never clobber fresher live data, never double-count a
message it has already handled, and never leave flags changed.
"""

from __future__ import annotations

import email
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.parcelmon.coordinator import ParcelmonCoordinator
from custom_components.parcelmon.imap_client import ImapSettings
from custom_components.parcelmon.models import Parcel

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# pytest-asyncio runs in strict mode here, so the coroutine tests need marking.
pytestmark = pytest.mark.asyncio


def load(name: str):
    return email.message_from_bytes((FIXTURES / name).read_bytes())


class FakeHass:
    """Runs the executor job inline; the coordinator only ever awaits it."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeStore:
    """Counts saves so persistence can be asserted without touching disk."""

    def __init__(self) -> None:
        self.saves = 0

    def async_schedule_save(self, parcels, seen_message_ids) -> None:
        self.saves += 1


@pytest.fixture
def coordinator(monkeypatch) -> ParcelmonCoordinator:
    """A coordinator wired up far enough to run async_rescan."""
    obj = object.__new__(ParcelmonCoordinator)
    obj._parcels = {}
    obj._seen_message_ids = []
    obj.removed = set()
    obj.retire_days = 0  # off, so fixtures dated in the past survive the sweep
    obj.hass = FakeHass()
    obj.store = FakeStore()
    obj.settings = ImapSettings(
        host="imap.example.com",
        port=993,
        username="someone@example.com",
        password="x",
        folder="Parcels",
    )
    # async_set_updated_data belongs to DataUpdateCoordinator and needs a real
    # hass loop; the rescan logic under test does not depend on it.
    monkeypatch.setattr(
        ParcelmonCoordinator, "async_set_updated_data", lambda self, data: None
    )
    return obj


def stub_history(monkeypatch, messages):
    """Replace the IMAP fetch with a fixed list of (imap_id, message)."""
    calls = {}

    def _fake(settings, days, limit):
        calls["days"] = days
        calls["limit"] = limit
        return [(str(i).encode(), m) for i, m in enumerate(messages, start=1)]

    monkeypatch.setattr(
        "custom_components.parcelmon.coordinator.fetch_history", _fake
    )
    return calls


class TestRescan:
    async def test_finds_parcels_in_already_read_mail(self, coordinator, monkeypatch):
        stub_history(monkeypatch, [load("auspost_in_transit.eml")])
        result = await coordinator.async_rescan()
        assert result["matched"] == 1
        assert result["new_parcels"] == 1
        assert result["tracked"] == 1

    async def test_unmatched_mail_is_counted_but_not_stored(
        self, coordinator, monkeypatch
    ):
        junk = email.message_from_string(
            "From: newsletter@example.com\nSubject: Quarterly update\n\nhello"
        )
        stub_history(monkeypatch, [junk])
        result = await coordinator.async_rescan()
        assert result["scanned"] == 1
        assert result["matched"] == 0
        assert coordinator._parcels == {}

    async def test_is_idempotent(self, coordinator, monkeypatch):
        """Message-ID de-duplication means a second run adds nothing."""
        stub_history(monkeypatch, [load("tge_delivered.eml")])
        first = await coordinator.async_rescan()
        second = await coordinator.async_rescan()
        assert first["new_parcels"] == 1
        assert second["new_parcels"] == 0
        assert second["tracked"] == first["tracked"]

    async def test_does_not_clobber_fresher_live_data(self, coordinator, monkeypatch):
        """A historical email must not overwrite what polling already learned."""
        message = load("auspost_in_transit.eml")
        # Seed the parcel as delivered, seen just now — newer than the fixture.
        from custom_components.parcelmon.parsers import parse_message

        parsed = parse_message(message)
        live = Parcel(
            carrier=parsed.carrier,
            tracking=parsed.tracking,
            status="delivered",
            seen_at=datetime.now(UTC),
        )
        coordinator._parcels[live.uid] = live

        stub_history(monkeypatch, [message])
        await coordinator.async_rescan()
        assert coordinator._parcels[live.uid].status == "delivered"

    async def test_parcel_is_dated_by_the_email_not_the_scan(
        self, coordinator, monkeypatch
    ):
        """Otherwise retire_days would measure from the rescan, not delivery."""
        stub_history(monkeypatch, [load("tge_delivered.eml")])
        await coordinator.async_rescan()
        parcel = next(iter(coordinator._parcels.values()))
        assert parcel.email_date is not None
        assert parcel.seen_at == parcel.email_date
        assert parcel.seen_at < datetime.now(UTC) - timedelta(days=1)

    async def test_results_are_persisted(self, coordinator, monkeypatch):
        """Otherwise a restart would lose everything the rescan just imported."""
        stub_history(monkeypatch, [load("auspost_in_transit.eml")])
        await coordinator.async_rescan()
        assert coordinator.store.saves == 1

    async def test_window_arguments_reach_the_fetch(self, coordinator, monkeypatch):
        calls = stub_history(monkeypatch, [])
        await coordinator.async_rescan(days=7, limit=50)
        assert calls == {"days": 7, "limit": 50}

    async def test_retires_parcels_that_finished_long_ago(
        self, coordinator, monkeypatch
    ):
        """Scanning old mail should not resurrect parcels already past retirement."""
        coordinator.retire_days = 3
        stub_history(monkeypatch, [load("tge_delivered.eml")])
        result = await coordinator.async_rescan()
        assert result["matched"] == 1
        assert coordinator._parcels == {}
