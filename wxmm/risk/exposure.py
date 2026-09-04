"""Exposure aggregated by Underlying. No cross-underlying netting."""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.core.errors import NonFungibleNettingError
from wxmm.core.underlying import Underlying
from wxmm.risk.position import Position


@dataclass(frozen=True, slots=True)
class Exposure:
    underlying: Underlying
    net_quantity: int
    gross_quantity: int
    n_positions: int


def aggregate(positions: tuple[Position, ...] | list[Position]) -> tuple[Exposure, ...]:
    buckets: dict[Underlying, list[Position]] = {}
    for pos in positions:
        buckets.setdefault(pos.underlying, []).append(pos)
    out: list[Exposure] = []
    for underlying, group in buckets.items():
        net = sum(item.quantity for item in group)
        gross = sum(abs(item.quantity) for item in group)
        out.append(
            Exposure(
                underlying=underlying,
                net_quantity=net,
                gross_quantity=gross,
                n_positions=len(group),
            )
        )
    return tuple(out)


def net_same_underlying(a: Position, b: Position) -> int:
    if not a.underlying.fungible(b.underlying):
        raise NonFungibleNettingError(
            f"cannot net {a.underlying} against {b.underlying}; cross-venue is basis"
        )
    return a.quantity + b.quantity
