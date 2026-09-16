"""Fill engine: last-in-queue, partials, SIZE_UNKNOWN, touch vs through."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from wxmm.backtest.fills import last_in_queue_fill
from wxmm.core.errors import SizeUnknownFlag
from wxmm.core.types import BookLevel, BookSnapshot, Order, Trade

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def _order(qty: int = 5) -> Order:
    return Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=qty,
        is_taker=False,
    )


def _book(*, size: int | None, known: bool, reconstructed: bool = False) -> BookSnapshot:
    return BookSnapshot(
        market_id="M",
        valid_at=TS,
        available_at=TS,
        bids=(BookLevel(39, 10),),
        asks=(BookLevel(40, size),),
        volume=0,
        ask_size_known=known,
        reconstructed=reconstructed,
        staleness=timedelta(seconds=90),
        two_sided=True,
    )


def _trade(size: int, price: int = 40) -> Trade:
    return Trade(market_id="M", ts=TS, available_at=TS, price_cents=price, size=size)


def _independent_through_qty(order: Order, book: BookSnapshot, trades: list[Trade]) -> int:
    """Oracle independent of ``last_in_queue_fill``.

    Touch (trade *at* the resting price) consumes displayed queue only.
    Through (trade *beyond* the resting price) fills the order in full.
    An optimistic engine that treats touch as through will diverge.
    """
    needs_ask = order.side == "buy"
    levels = book.asks if needs_ask else book.bids
    displayed = 0
    for level in levels:
        if level.price_cents == order.price_cents:
            displayed = 0 if level.size is None else level.size
            break
    at_level = 0
    through = 0
    for trade in trades:
        if trade.market_id != order.market_id:
            continue
        if order.side == "buy":
            if trade.price_cents < order.price_cents:
                through += trade.size
            elif trade.price_cents == order.price_cents:
                at_level += trade.size
        elif trade.price_cents > order.price_cents:
            through += trade.size
        elif trade.price_cents == order.price_cents:
            at_level += trade.size
    if through > 0:
        return order.quantity
    remainder = at_level - displayed
    if remainder <= 0:
        return 0
    return min(order.quantity, remainder)


def test_no_fill_until_queue_trades_through() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=10, known=True), [_trade(10)])
    assert fill.status == "none"
    assert fill.filled_qty == 0
    assert fill.queue_ahead == 10


def test_touch_equal_to_queue_does_not_fill() -> None:
    """Optimistic fill-on-touch would fill here. Through-only must not."""
    fill = last_in_queue_fill(_order(5), _book(size=10, known=True), [_trade(10, price=40)])
    assert fill.filled_qty == 0
    assert fill.status == "none"


def test_partial_after_queue_consumed() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=10, known=True), [_trade(12)])
    assert fill.status == "partial"
    assert fill.filled_qty == 2


def test_full_fill_when_level_trades_through() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=10, known=True), [_trade(3, price=39)])
    assert fill.status == "filled"
    assert fill.filled_qty == 5
    assert fill.reconstructed is False
    assert fill.staleness == timedelta(seconds=90)


def test_randomized_touch_vs_through_matches_independent_oracle() -> None:
    rng = random.Random(7)
    book = _book(size=10, known=True)
    order = _order(5)
    through_fills = 0
    sequences = 400
    for _ in range(sequences):
        n_trades = rng.randint(0, 8)
        trades = []
        for _ in range(n_trades):
            kind = rng.choice(("touch", "through", "away"))
            if kind == "touch":
                px = 40
            elif kind == "through":
                px = rng.choice((38, 39))
            else:
                px = rng.choice((41, 42, 43))
            trades.append(_trade(rng.randint(1, 12), price=px))
        expected = _independent_through_qty(order, book, trades)
        fill = last_in_queue_fill(order, book, trades)
        assert fill.filled_qty == expected
        if expected > 0:
            through_fills += 1
    assert through_fills > 0, "oracle sequences produced no fills; test is meaningless"


def test_size_unknown_not_guessed() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=None, known=False), [_trade(100, price=39)])
    assert fill.status == "SIZE_UNKNOWN"
    assert fill.filled_qty == 0
    assert fill.queue_ahead is None
    with pytest.raises(SizeUnknownFlag):
        fill.assert_executable()
