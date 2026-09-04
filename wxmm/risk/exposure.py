"""Exposure in probability space and dollars. Per Underlying, never summed across.

Allowed to assume
    Each Underlying is reported separately: implied p per bracket, notional,
    collateral committed, max loss.

Must never
    Sum dollars or probabilities across non-fungible Underlyings. Net Kalshi
    NYC against Polymarket NYC.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from wxmm.core.errors import NonFungibleNettingError
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying
from wxmm.risk.position import Position, require_fungible


@dataclass(frozen=True, slots=True)
class BracketProb:
    market_id: str
    implied_p: Decimal | None  # None = missing quote, not zero


@dataclass(frozen=True, slots=True)
class UnderlyingExposure:
    underlying: Underlying
    venue: str
    probability_space: tuple[BracketProb, ...]
    notional: Money
    collateral_committed: Money
    max_loss: Money
    net_quantity: int
    gross_quantity: int
    n_positions: int


@dataclass(frozen=True, slots=True)
class Exposure:
    """Legacy alias used by tests: quantity-only slice of UnderlyingExposure."""

    underlying: Underlying
    net_quantity: int
    gross_quantity: int
    n_positions: int


def by_underlying(
    positions: Sequence[Position] | Iterable[Position],
    *,
    implied_p: Mapping[tuple[str, str], Decimal | None] | None = None,
    marks_cents: Mapping[tuple[str, str], int] | None = None,
) -> tuple[UnderlyingExposure, ...]:
    """One exposure row per Underlying group. Non-fungible lots stay separate.

    Positions whose Underlying is not fungible with any other (including
    itself, e.g. Polymarket unverified day convention) each get their own row.
    """
    implied_p = implied_p or {}
    marks_cents = marks_cents or {}
    groups: list[list[Position]] = []
    for pos in positions:
        placed = False
        for group in groups:
            if pos.underlying.fungible(group[0].underlying):
                group.append(pos)
                placed = True
                break
        if not placed:
            groups.append([pos])
    out: list[UnderlyingExposure] = []
    for group in groups:
        if len(group) > 1:
            require_fungible(group)
        net = sum(item.quantity for item in group)
        gross = sum(abs(item.quantity) for item in group)
        notional = Money.zero()
        for item in group:
            mark = marks_cents.get(item.key, item.avg_price_cents)
            if mark is not None:
                notional = notional + Money.cents(item.quantity * mark)
        probs = tuple(
            BracketProb(market_id=item.market_id, implied_p=implied_p.get(item.key))
            for item in group
        )
        collateral = Money(abs(notional.amount))
        max_loss = Money.cents(sum(abs(item.quantity) * 100 for item in group))
        out.append(
            UnderlyingExposure(
                underlying=group[0].underlying,
                venue=group[0].venue,
                probability_space=probs,
                notional=notional,
                collateral_committed=collateral,
                max_loss=max_loss,
                net_quantity=net,
                gross_quantity=gross,
                n_positions=len(group),
            )
        )
    return tuple(out)


def aggregate(positions: tuple[Position, ...] | list[Position]) -> tuple[Exposure, ...]:
    """Quantity-only view. Still refuses to merge non-fungible Underlyings."""
    return tuple(
        Exposure(
            underlying=row.underlying,
            net_quantity=row.net_quantity,
            gross_quantity=row.gross_quantity,
            n_positions=row.n_positions,
        )
        for row in by_underlying(positions)
    )


def net_same_underlying(a: Position, b: Position) -> int:
    if not a.underlying.fungible(b.underlying):
        raise NonFungibleNettingError(
            f"cannot net {a.underlying} against {b.underlying}; cross-venue is basis"
        )
    return a.quantity + b.quantity


def sum_notional_across(exposures: Sequence[UnderlyingExposure]) -> Money:
    """Forbidden unless every row is fungible with every other."""
    if not exposures:
        return Money.zero()
    positions = [
        Position(
            venue=row.venue,
            market_id="agg",
            underlying=row.underlying,
            quantity=row.net_quantity,
            avg_price_cents=0,
        )
        for row in exposures
    ]
    require_fungible(positions)
    total = Money.zero()
    for row in exposures:
        total = total + row.notional
    return total
