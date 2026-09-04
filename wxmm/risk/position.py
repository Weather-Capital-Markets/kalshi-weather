"""Positions. Quantity is signed; missing is not a zero position."""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.core.money import Money
from wxmm.core.underlying import Underlying


@dataclass(frozen=True, slots=True)
class Position:
    venue: str
    market_id: str
    underlying: Underlying
    quantity: int
    avg_price_cents: int | None  # None = unknown average, not zero

    def notional(self, mark_cents: int | None = None) -> Money | None:
        price = mark_cents if mark_cents is not None else self.avg_price_cents
        if price is None:
            return None
        return Money.cents(self.quantity * price)
