"""Decision engine: NullFairValue, RATE_BLOCKED, HALT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.core.types import FrozenClock
from wxmm.decide.engine import propose
from wxmm.decide.fairvalue import NullFairValue
from wxmm.live.ratelimit import kalshi_write_bucket
from wxmm.strategy.view import BookView, MarketView

UTC = timezone.utc


def _view() -> MarketView:
    return MarketView(
        books=(
            BookView(
                venue="kalshi",
                market_id="M",
                two_sided=True,
                bid_cents=40,
                ask_cents=42,
                bid_size=5,
                ask_size=5,
                ask_size_known=True,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
        ),
        positions=(),
        fills=(),
    )


def test_null_fair_value_still_emits_quotes() -> None:
    view = _view()
    assert NullFairValue().fair(view) is None
    out = propose(view)
    assert out
    assert all(p.edge is None for p in out)
    assert all("no fair value" in p.rationale for p in out)
    assert {p.side for p in out} == {"buy", "sell"}


def test_rate_blocked_when_bucket_empty() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    bucket = kalshi_write_bucket(clock)
    while bucket.consume(10.0):
        pass
    out = propose(_view(), buckets={"kalshi": bucket})
    assert out
    assert all(p.rate_blocked for p in out)


def test_one_order_budget_blocks_second_ranked_quote_without_consuming() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    bucket = kalshi_write_bucket(clock)
    for _ in range(9):
        assert bucket.consume(10.0)
    assert bucket.remaining() == pytest.approx(10.0)
    out = propose(_view(), buckets={"kalshi": bucket})
    sendable = [p for p in out if not p.rate_blocked]
    blocked = [p for p in out if p.rate_blocked]
    assert len(sendable) == 1
    assert sendable[0].side == "buy"
    assert blocked
    assert all("; RATE_BLOCKED" in p.rationale for p in blocked)
    assert bucket.remaining() == pytest.approx(10.0)


def test_degraded_stamps_proposals() -> None:
    out = propose(_view(), system_status="DEGRADED", degradation=("feed_down:kalshi",))
    assert out
    assert all(p.degradation == ("feed_down:kalshi",) for p in out)
