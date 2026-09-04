"""Fill engine: last-in-queue, partials, SIZE_UNKNOWN."""

from __future__ import annotations

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


def test_no_fill_until_queue_trades_through() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=10, known=True), [_trade(10)])
    assert fill.status == "none"
    assert fill.filled_qty == 0
    assert fill.queue_ahead == 10


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


def test_size_unknown_not_guessed() -> None:
    fill = last_in_queue_fill(_order(5), _book(size=None, known=False), [_trade(100, price=39)])
    assert fill.status == "SIZE_UNKNOWN"
    assert fill.filled_qty == 0
    assert fill.queue_ahead is None
    with pytest.raises(SizeUnknownFlag):
        fill.assert_executable()
