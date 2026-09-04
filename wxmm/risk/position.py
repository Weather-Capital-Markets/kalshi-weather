"""Positions keyed by (venue, market). Each carries its Underlying.

Allowed to assume
    A position is identified by ``(venue, market_id)``. Quantity is signed.
    Missing average price is not zero.

Must never
    Aggregate or net across non-fungible Underlyings (raises). Treat two
    Polymarket NYC positions as fungible while day convention is unverified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from wxmm.core.errors import NonFungibleNettingError
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying


@dataclass(frozen=True, slots=True)
class Position:
    venue: str
    market_id: str
    underlying: Underlying
    quantity: int
    avg_price_cents: int | None  # None = unknown average, not zero

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue, self.market_id)

    def notional(self, mark_cents: int | None = None) -> Money | None:
        price = mark_cents if mark_cents is not None else self.avg_price_cents
        if price is None:
            return None
        return Money.cents(self.quantity * price)


def require_fungible(positions: Iterable[Position]) -> None:
    items = list(positions)
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            if not left.underlying.fungible(right.underlying):
                raise NonFungibleNettingError(
                    f"cannot aggregate {left.venue}:{left.market_id} "
                    f"({left.underlying}) with {right.venue}:{right.market_id} "
                    f"({right.underlying}); not fungible"
                )


def net_quantity(positions: Iterable[Position]) -> int:
    """Sum signed qty. Raises unless every pair of Underlyings is fungible()."""
    items = list(positions)
    require_fungible(items)
    return sum(item.quantity for item in items)


class PositionBook:
    """One lot per ``(venue, market_id)``. No silent merge across underlyings."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Position] = {}

    def get(self, venue: str, market_id: str) -> Position | None:
        return self._items.get((venue, market_id))

    def upsert(self, position: Position) -> None:
        key = position.key
        existing = self._items.get(key)
        # Same (venue, market) lot may update even when fungible() is False
        # (unverified fields). Distinct Underlying field-sets cannot share a key.
        if existing is not None and existing.underlying != position.underlying:
            raise NonFungibleNettingError(
                f"cannot replace {key} underlying {existing.underlying} "
                f"with {position.underlying}"
            )
        self._items[key] = position

    def add_fill(self, position: Position) -> Position:
        """Add quantity onto the same (venue, market) key."""
        existing = self.get(position.venue, position.market_id)
        if existing is None:
            self.upsert(position)
            return position
        if existing.underlying != position.underlying:
            raise NonFungibleNettingError(
                f"cannot add fill on {position.key}: underlyings differ"
            )
        qty = existing.quantity + position.quantity
        merged = Position(
            venue=existing.venue,
            market_id=existing.market_id,
            underlying=existing.underlying,
            quantity=qty,
            avg_price_cents=existing.avg_price_cents,
        )
        self.upsert(merged)
        return merged

    def all(self) -> tuple[Position, ...]:
        return tuple(self._items.values())

    def __iter__(self) -> Iterator[Position]:
        return iter(self._items.values())

    def total_quantity(self) -> int:
        return net_quantity(self.all())
