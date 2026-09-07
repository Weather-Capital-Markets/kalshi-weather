"""Fake venue: scripted BookUpdates, get_venue('fake'), no HTTP."""

from __future__ import annotations

from datetime import datetime, timezone

from wxmm.core.book import book_store_key, snapshot_payload, snapshot_update
from wxmm.core.types import ClockBoundStore, FrozenClock, InMemoryAsOfStore
from wxmm.core.view import build_market_view
from wxmm.venues.base import get_venue
from wxmm.venues.fake.adapter import FakeVenue

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def test_get_venue_fake_is_in_memory() -> None:
    venue = get_venue("fake", InMemoryAsOfStore())
    assert venue.name == "fake"
    limits = venue.rate_limits()
    assert limits["write_tokens_per_sec"] == 100.0
    assert limits["batch_cancel_tokens"] == 2.0


def test_fake_venue_emits_scripted_book_updates_onto_store() -> None:
    store = InMemoryAsOfStore()
    update = snapshot_update(
        venue="fake",
        market_id="M",
        valid_at=TS,
        available_at=TS,
        payload=snapshot_payload(
            market_id="M", bid_cents=40, ask_cents=42, bid_size=5, ask_size=5
        ),
        source="script",
    )
    venue = FakeVenue(store, tape=(update,))
    emitted = venue.emit_book_updates()
    assert emitted == (update,)
    rec = store.get(book_store_key("fake", "M"), as_of=TS)
    assert rec.available_at == TS
    clock = FrozenClock(TS)
    view = build_market_view(
        store=ClockBoundStore(store, clock),
        clock=clock,
        book_keys=(("fake", book_store_key("fake", "M")),),
    )
    assert view.books[0].bid_cents == 40
    assert view.books[0].ask_cents == 42
