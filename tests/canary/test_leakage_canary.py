"""Leakage canary: a cheating book/outcome read must raise LeakageError.

Honest as-of reads against the same store must succeed. This file is the
Stage B1 gate: no second domain module until this distinction holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.core.errors import LeakageError, MissingDataError
from wxmm.core.types import (
    AsOfRecord,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
    ReadContext,
)

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
    """Test-only. Lives in tests/, never in wxmm/strategy/."""

    def read_future_outcome(self, ctx: ReadContext) -> object:
        future = ctx.clock.now() + timedelta(days=1)
        return ctx.store.get(OUTCOME_KEY, as_of=future).payload

    def read_future_book(self, ctx: ReadContext) -> object:
        future = ctx.clock.now() + timedelta(hours=6)
        return ctx.store.get(BOOK_KEY, as_of=future).payload


class HonestStrategy:
    def read_book(self, ctx: ReadContext) -> object:
        return ctx.store.get(BOOK_KEY, as_of=ctx.clock.now()).payload

    def read_outcome(self, ctx: ReadContext) -> object:
        return ctx.store.get(OUTCOME_KEY, as_of=ctx.clock.now()).payload


def test_cheating_strategy_reading_future_outcome_raises_leakage() -> None:
    store, clock = _store_with_book_and_future_outcome()
    ctx = ReadContext(clock=clock, store=store)
    with pytest.raises(LeakageError, match="after clock.now"):
        CheatingStrategy().read_future_outcome(ctx)


def test_cheating_strategy_reading_book_after_clock_raises_leakage() -> None:
    store, clock = _store_with_book_and_future_outcome()
    ctx = ReadContext(clock=clock, store=store)
    with pytest.raises(LeakageError, match="after clock.now"):
        CheatingStrategy().read_future_book(ctx)


def test_honest_strategy_reads_available_book() -> None:
    store, clock = _store_with_book_and_future_outcome()
    ctx = ReadContext(clock=clock, store=store)
    payload = HonestStrategy().read_book(ctx)
    assert payload == {"yes_bid_cents": 40, "yes_ask_cents": 42, "ask_size": None}


def test_honest_strategy_cannot_see_unavailable_outcome() -> None:
    store, clock = _store_with_book_and_future_outcome()
    ctx = ReadContext(clock=clock, store=store)
    with pytest.raises(MissingDataError, match="outcome:2026-07-04"):
        HonestStrategy().read_outcome(ctx)


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
