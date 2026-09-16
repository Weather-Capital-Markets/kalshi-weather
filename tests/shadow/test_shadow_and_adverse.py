"""Shadow last-in-queue vs reality. Touch-not-through must not paper-fill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxmm.core.types import BookLevel, BookSnapshot, Order, Trade
from wxmm.measure.adverse import distribution, markout_cents, marks_for_fill
from wxmm.measure.shadow import compare_quote, report

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def _book(size: int) -> BookSnapshot:
    # Resting buy at 40 sits behind the ask queue at 40 (B1 last_in_queue).
    return BookSnapshot(
        market_id="M",
        valid_at=TS,
        available_at=TS,
        bids=(BookLevel(39, 10),),
        asks=(BookLevel(40, size),),
        volume=10,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
        source="shadow",
    )


def _order() -> Order:
    return Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=5,
        is_taker=False,
    )


def _trade(size: int, price: int, *, sec: int = 0) -> Trade:
    ts = TS + timedelta(seconds=sec)
    return Trade(market_id="M", ts=ts, available_at=ts, price_cents=price, size=size)


def test_touch_not_through_paper_does_not_fill() -> None:
    # Displayed queue is 10; 10 contracts print at our price — queue only.
    obs = compare_quote(_order(), _book(10), [_trade(10, 40)], real_filled_qty=0)
    assert obs.paper.filled_qty == 0
    assert obs.paper.status == "none"
    assert obs.optimism is False


def test_mixed_touch_through_matches_independent_through_count() -> None:
    trades = [
        _trade(10, 40, sec=1),  # touch, consumes queue
        _trade(3, 39, sec=2),  # through
        _trade(2, 40, sec=3),  # more at level after through already filled us
    ]
    through = sum(t.size for t in trades if t.price_cents < 40)
    obs = compare_quote(_order(), _book(10), trades, real_filled_qty=5)
    assert through > 0
    assert obs.paper.filled_qty == 5
    assert obs.paper.status == "filled"
    # Independent through count is 3; last-in-queue fills in full once through>0.
    assert obs.paper.filled_qty == _order().quantity


def test_optimism_and_pessimism_flags() -> None:
    paper_only = compare_quote(_order(), _book(10), [_trade(3, 39)], real_filled_qty=0)
    assert paper_only.optimism is True
    real_only = compare_quote(_order(), _book(10), [_trade(10, 40)], real_filled_qty=5)
    assert real_only.pessimism is True
    summary = report([paper_only, real_only])
    assert summary.optimism_count == 1
    assert summary.pessimism_count == 1
    assert summary.paper_fill_count == 1
    assert summary.real_fill_count == 1


def test_markout_hand_computed() -> None:
    assert markout_cents(side="buy", fill_price_cents=40, mid_cents=42) == 2
    assert markout_cents(side="sell", fill_price_cents=42, mid_cents=40) == 2
    fill_ts = TS
    mids = {
        fill_ts + timedelta(minutes=1): 42,
        fill_ts + timedelta(minutes=5): 38,
        fill_ts + timedelta(minutes=30): 41,
    }
    marks = marks_for_fill(
        fill_id="f1",
        fill_ts=fill_ts,
        fill_price_cents=40,
        side="buy",
        mids=mids,
        settlement_mid_cents=50,
    )
    by_h = {m.horizon: m.markout_cents for m in marks}
    assert by_h["1m"] == 2
    assert by_h["5m"] == -2
    assert by_h["30m"] == 1
    assert by_h["settlement"] == 10
    dist = distribution(marks, "1m")
    assert dist.fill_count == 1
    assert dist.markouts_cents == (2,)
    assert dist.mean_cents() == 2
