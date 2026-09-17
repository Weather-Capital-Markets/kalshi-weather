"""Frozen MarketView from a recorded BookUpdate tape."""

from __future__ import annotations

from dataclasses import astuple
from datetime import datetime, timedelta, timezone

from wxmm.core.book import apply_book_update, book_store_key, snapshot_payload, snapshot_update
from wxmm.core.types import ClockBoundStore, FrozenClock, InMemoryAsOfStore
from wxmm.core.view import build_market_view

UTC = timezone.utc
T0 = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
KEY = book_store_key("kalshi", "KXHIGHNY-26JUL04-T90")


def test_golden_recorded_tape_market_view_tuple() -> None:
    clock = FrozenClock(T0)
    store = ClockBoundStore(InMemoryAsOfStore(), clock)
    apply_book_update(
        store,
        snapshot_update(
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-T90",
            valid_at=T0,
            available_at=T0,
            payload=snapshot_payload(
                market_id="KXHIGHNY-26JUL04-T90",
                bid_cents=40,
                ask_cents=42,
                bid_size=5,
                ask_size=5,
                volume=1,
                two_sided=True,
            ),
            source="golden",
        ),
    )
    view = build_market_view(store=store, clock=clock, book_keys=(("kalshi", KEY),))
    assert astuple(view) == (
        (
            (
                "kalshi",
                "KXHIGHNY-26JUL04-T90",
                True,
                40,
                42,
                5,
                5,
                True,
                1,
                False,
                timedelta(0),
            ),
        ),
        (),
        (),
    )
