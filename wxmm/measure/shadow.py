"""Shadow last-in-queue vs real resting fills.

Allowed to assume
    The paper model is ``last_in_queue_fill``. Persist both series.

Must never
    Count a touch-not-through as a paper fill. Drop the fill count.
    Import ``wxmm.live`` transports.
"""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.backtest.fills import Fill, last_in_queue_fill
from wxmm.core.types import BookSnapshot, Order, Trade


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    paper: Fill
    real_filled_qty: int
    optimism: bool
    pessimism: bool


@dataclass(frozen=True, slots=True)
class ShadowReport:
    observations: tuple[ShadowObservation, ...]
    optimism_count: int
    pessimism_count: int
    paper_fill_count: int
    real_fill_count: int


def compare_quote(
    order: Order,
    book: BookSnapshot,
    trades: tuple[Trade, ...] | list[Trade],
    real_filled_qty: int,
) -> ShadowObservation:
    paper = last_in_queue_fill(order, book, trades)
    paper_qty = paper.filled_qty
    return ShadowObservation(
        paper=paper,
        real_filled_qty=real_filled_qty,
        optimism=paper_qty > 0 and real_filled_qty == 0,
        pessimism=real_filled_qty > 0 and paper_qty == 0,
    )


def report(observations: tuple[ShadowObservation, ...] | list[ShadowObservation]) -> ShadowReport:
    items = tuple(observations)
    return ShadowReport(
        observations=items,
        optimism_count=sum(1 for item in items if item.optimism),
        pessimism_count=sum(1 for item in items if item.pessimism),
        paper_fill_count=sum(1 for item in items if item.paper.filled_qty > 0),
        real_fill_count=sum(1 for item in items if item.real_filled_qty > 0),
    )
