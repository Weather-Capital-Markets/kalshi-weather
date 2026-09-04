"""Random strategy expected P&L ≈ −(Kalshi taker costs)."""

from __future__ import annotations

import random
from decimal import Decimal

from wxmm.core.money import Money
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee


def test_zero_edge_monte_carlo_pnl_equals_negative_fees() -> None:
    rng = random.Random(0)
    price = Decimal("0.40")
    price_cents = 40
    fee = taker_fee(1, price, FeeRounding.PER_ORDER_CENT)
    n = 8000
    total = Decimal("0")
    for _ in range(n):
        win = rng.random() < float(price)
        settle_cents = 100 if win else 0
        pnl = Money.cents(settle_cents - price_cents) - fee
        total += pnl.amount
    mean = total / Decimal(n)
    # Fair binary at P: E[settle − price] = 0, so E[PnL] = −fee.
    err = abs(mean + fee.amount)
    assert err < Decimal("0.03")
