"""Empty-book 0/100 must not enter MarketView as a two-sided 99¢ spread."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxmm.core.types import (
    AsOfRecord,
    BookLevel,
    BookSnapshot,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
)
from wxmm.core.view import book_view_from_snapshot, build_market_view
from wxmm.live.state import LiveState, snapshot_payload, snapshot_update
from wxmm.venues.kalshi.adapter import KalshiVenue
from wxmm.venues.polymarket.adapter import PolymarketVenue

UTC = timezone.utc
T0 = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
KEY = "kalshi:book:M"


def test_raw_zero_one_hundred_payload_is_not_two_sided_in_view() -> None:
    clock = FrozenClock(T0)
    inner = InMemoryAsOfStore()
    inner.put(
        AsOfRecord(
            key=KEY,
            payload={
                "market_id": "M",
                "yes_bid_cents": 0,
                "yes_ask_cents": 100,
                "bid_size": 9,
                "ask_size": 8,
                "two_sided": True,
            },
            valid_at=T0,
            available_at=T0,
            source="thesis",
            ingest_run_id="thesis",
        )
    )
    view = build_market_view(
        store=ClockBoundStore(inner, clock),
        clock=clock,
        book_keys=(("kalshi", KEY),),
    )
    book = view.books[0]
    assert book.two_sided is False
    assert book.bid_cents is None
    assert book.ask_cents is None


def test_zero_bid_with_real_ask_is_one_sided() -> None:
    clock = FrozenClock(T0)
    live = LiveState(clock)
    live.apply(
        snapshot_update(
            venue="kalshi",
            market_id="M",
            valid_at=T0,
            available_at=T0,
            payload=snapshot_payload(
                market_id="M",
                bid_cents=0,
                ask_cents=55,
                bid_size=4,
                ask_size=6,
                two_sided=True,
            ),
            source="thesis",
        )
    )
    view = build_market_view(store=live, clock=clock, book_keys=(("kalshi", KEY),))
    book = view.books[0]
    assert book.two_sided is False
    assert book.bid_cents is None
    assert book.ask_cents == 55


def test_snapshot_zero_one_hundred_levels_are_clipped() -> None:
    snap = BookSnapshot(
        market_id="M",
        valid_at=T0,
        available_at=T0,
        bids=(BookLevel(0, 3),),
        asks=(BookLevel(100, 4),),
        volume=0,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
        source="thesis",
    )
    view = book_view_from_snapshot("kalshi", snap)
    assert view.two_sided is False
    assert view.bid_cents is None
    assert view.ask_cents is None


def test_kalshi_adapter_does_not_default_missing_to_empty_book_sentinel() -> None:
    inner = InMemoryAsOfStore()
    inner.put(
        AsOfRecord(
            key="kalshi:book:M",
            payload={"yes_bid_cents": 40, "bid_size": 2, "ask_size": 3, "volume": 1},
            valid_at=T0,
            available_at=T0,
            source="thesis",
            ingest_run_id="thesis",
        )
    )
    venue = KalshiVenue(inner)
    snap = venue.book_at("M", T0)
    assert snap.two_sided is False
    assert snap.bids[0].price_cents == 40
    assert snap.asks == ()


def test_polymarket_adapter_reads_yes_bid_cents_from_live_payload() -> None:
    inner = InMemoryAsOfStore()
    inner.put(
        AsOfRecord(
            key="polymarket:book:nyc-84-85",
            payload={
                "yes_bid_cents": 40,
                "yes_ask_cents": 41,
                "bid_size": 10,
                "ask_size": 9,
            },
            valid_at=T0,
            available_at=T0,
            source="thesis",
            ingest_run_id="thesis",
        )
    )
    venue = PolymarketVenue(inner)
    snap = venue.book_at("nyc-84-85", T0)
    assert snap.two_sided is True
    assert snap.bids[0].price_cents == 40
    assert snap.asks[0].price_cents == 41
