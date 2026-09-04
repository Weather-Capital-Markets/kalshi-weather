"""Random-settlement P&L ≈ −(Kalshi taker costs), conditional on fills occurring.

Fill-model optimism (fill-on-touch vs fill-on-through) is tested in
``test_fills.py``, not here. This file only checks fee arithmetic once fills
are known to have occurred.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from wxmm.backtest.fills import last_in_queue_fill
from wxmm.core.money import Money
from wxmm.core.types import BookLevel, BookSnapshot, Order, Trade
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def test_zero_edge_conditional_on_fills_pnl_equals_negative_fees() -> None:
    rng = random.Random(0)
    price = Decimal("0.40")
    p = float(price)
    price_cents = 40
    fee = taker_fee(1, price, FeeRounding.PER_ORDER_CENT)
    book = BookSnapshot(
        market_id="M",
        valid_at=TS,
        available_at=TS,
        bids=(BookLevel(39, 10),),
        asks=(BookLevel(40, 10),),
        volume=0,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
    )
    trades = (Trade(market_id="M", ts=TS, available_at=TS, price_cents=39, size=1),)
    n = 8000
    total = Decimal("0")
    fill_count = 0
    for _ in range(n):
        order = Order(
            venue="kalshi",
            market_id="M",
            side="buy",
            price_cents=price_cents,
            quantity=1,
            is_taker=False,
        )
        fill = last_in_queue_fill(order, book, trades)
        if fill.status not in {"filled", "partial"} or not fill.filled_qty:
            continue
        fill_count += fill.filled_qty
        win = rng.random() < p
        settle_cents = 100 if win else 0
        pnl = Money.cents((settle_cents - price_cents) * fill.filled_qty) - fee
        total += pnl.amount
    assert fill_count > 0, "zero-edge test is meaningless if nothing filled"
    mean = total / Decimal(fill_count)
    err = abs(mean + fee.amount)
    se = Decimal(str(math.sqrt(p * (1.0 - p) / fill_count)))
    tolerance = se * 6
    assert err < tolerance, f"mean error {err} exceeds 6σ={tolerance} at n={fill_count}"
