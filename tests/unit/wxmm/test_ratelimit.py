"""Rate-limit buckets against documented venue budgets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxmm.core.types import FrozenClock
from wxmm.live.ratelimit import (
    KALSHI_BATCH_CANCEL_COST,
    KALSHI_ORDER_COST,
    KALSHI_WRITE_TOKENS_PER_SEC,
    POLYMARKET_REQUESTS_PER_SEC,
    kalshi_write_bucket,
    polymarket_public_bucket,
    proposal_cost,
)

UTC = timezone.utc


def test_kalshi_bucket_matches_documented_write_budget() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    bucket = kalshi_write_bucket(clock)
    assert bucket.remaining() == KALSHI_WRITE_TOKENS_PER_SEC
    assert proposal_cost("kalshi") == KALSHI_ORDER_COST
    assert proposal_cost("kalshi", batch_cancel=True) == KALSHI_BATCH_CANCEL_COST
    sent = 0
    while bucket.consume(KALSHI_ORDER_COST):
        sent += 1
    assert sent == 10
    assert not bucket.can_afford(KALSHI_ORDER_COST)
    clock.set(clock.now() + timedelta(seconds=1))
    assert bucket.can_afford(KALSHI_ORDER_COST)
    assert bucket.consume(KALSHI_BATCH_CANCEL_COST)
    assert bucket.consume(KALSHI_BATCH_CANCEL_COST)


def test_polymarket_bucket_20_per_second() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    bucket = polymarket_public_bucket(clock)
    n = 0
    while bucket.consume(1.0):
        n += 1
    assert n == int(POLYMARKET_REQUESTS_PER_SEC)
