"""Leakage canary: cheating reads fail at the strategy boundary and/or LeakageError.

A strategy receives only a frozen ``MarketView``. It must not be able to reach
the data store, venue adapters, settlement, or clock. The harness builds the
view from as-of-safe data only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.backtest.harness import build_market_view
from wxmm.core.errors import LeakageError, MissingDataError
from wxmm.core.types import (
    AsOfRecord,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
)
from wxmm.strategy.view import MarketView, ProposedOrder

UTC = timezone.utc

BOOK_KEY = "book:KXHIGHNY-26JUL04-T90"
OUTCOME_KEY = "outcome:2026-07-04"


def _store_with_book_and_future_outcome() -> tuple[ClockBoundStore, FrozenClock]:
    clock = FrozenClock(datetime(2026, 7, 4, 18, 0, tzinfo=UTC))
    inner = InMemoryAsOfStore()
    inner.put(
        AsOfRecord(
            key=BOOK_KEY,
            payload={"yes_bid_cents": 40, "yes_ask_cents": 42, "ask_size": None},
            valid_at=datetime(2026, 7, 4, 16, 0, tzinfo=UTC),
            available_at=datetime(2026, 7, 4, 16, 0, tzinfo=UTC),
            source="canary",
            ingest_run_id="canary-0",
        )
    )
    inner.put(
        AsOfRecord(
            key=OUTCOME_KEY,
            payload={"high_f": 91},
            valid_at=datetime(2026, 7, 5, 12, 4, tzinfo=UTC),
            available_at=datetime(2026, 7, 5, 12, 4, tzinfo=UTC),
            source="canary",
            ingest_run_id="canary-0",
        )
    )
    return ClockBoundStore(inner, clock), clock


class CheatingStrategy:
    """Test-only. Lives in tests/, never in wxmm/strategy/ or strategies/."""

    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        store = getattr(view, "store", None)
        if store is not None:
            future = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
            store.get(OUTCOME_KEY, as_of=future)
        return []

    def try_future_as_of(self, view: MarketView) -> object:
        return view.store.get(OUTCOME_KEY, as_of=datetime(2026, 7, 6, tzinfo=UTC))  # type: ignore[attr-defined]


class HonestStrategy:
    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        _ = view.books
        return []


def test_market_view_has_no_store_clock_venue_or_settlement() -> None:
    view = MarketView(books=(), positions=(), fills=())
    for name in ("store", "clock", "venue", "settlement"):
        assert not hasattr(view, name)


def test_cheating_strategy_cannot_reach_store_from_view() -> None:
    store, clock = _store_with_book_and_future_outcome()
    view = build_market_view(store=store, clock=clock, book_keys=[("kalshi", BOOK_KEY)])
    cheating = CheatingStrategy()
    assert cheating.on_snapshot(view) == []
    with pytest.raises(AttributeError):
        cheating.try_future_as_of(view)


def test_harness_future_as_of_raises_leakage() -> None:
    store, clock = _store_with_book_and_future_outcome()
    future = clock.now() + timedelta(days=1)
    with pytest.raises(LeakageError, match="after clock.now"):
        store.get(OUTCOME_KEY, as_of=future)


def test_harness_book_after_clock_raises_leakage() -> None:
    store, clock = _store_with_book_and_future_outcome()
    future = clock.now() + timedelta(hours=6)
    with pytest.raises(LeakageError, match="after clock.now"):
        store.get(BOOK_KEY, as_of=future)


def test_honest_strategy_sees_available_book_via_market_view() -> None:
    store, clock = _store_with_book_and_future_outcome()
    view = build_market_view(store=store, clock=clock, book_keys=[("kalshi", BOOK_KEY)])
    assert HonestStrategy().on_snapshot(view) == []
    assert len(view.books) == 1
    assert view.books[0].bid_cents == 40
    assert view.books[0].ask_cents == 42


def test_honest_strategy_cannot_see_unavailable_outcome_in_view() -> None:
    store, clock = _store_with_book_and_future_outcome()
    view = build_market_view(store=store, clock=clock, book_keys=[("kalshi", BOOK_KEY)])
    keys = [book.market_id for book in view.books]
    assert OUTCOME_KEY not in keys
    assert all("outcome" not in book.market_id for book in view.books)
    with pytest.raises(MissingDataError, match="outcome:2026-07-04"):
        store.get(OUTCOME_KEY, as_of=clock.now())


def test_harness_tainted_future_book_is_not_in_view() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 18, 0, tzinfo=UTC))
    inner = InMemoryAsOfStore()
    inner.put(
        AsOfRecord(
            key=BOOK_KEY,
            payload={"yes_bid_cents": 99, "yes_ask_cents": 100},
            valid_at=datetime(2026, 7, 4, 22, 0, tzinfo=UTC),
            available_at=datetime(2026, 7, 4, 22, 0, tzinfo=UTC),
            source="canary",
            ingest_run_id="canary-0",
        )
    )
    store = ClockBoundStore(inner, clock)
    with pytest.raises(MissingDataError):
        build_market_view(store=store, clock=clock, book_keys=[("kalshi", BOOK_KEY)])


def test_get_record_raises_leakage_rather_than_skipping() -> None:
    store, clock = _store_with_book_and_future_outcome()
    future = AsOfRecord(
        key=OUTCOME_KEY,
        payload={"high_f": 91},
        valid_at=datetime(2026, 7, 5, 12, 4, tzinfo=UTC),
        available_at=datetime(2026, 7, 5, 12, 4, tzinfo=UTC),
        source="canary",
        ingest_run_id="canary-0",
    )
    with pytest.raises(LeakageError, match="available_at"):
        store.get_record(future, as_of=clock.now())
