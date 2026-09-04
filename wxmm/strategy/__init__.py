"""Strategy Protocol only. Zero production implementations.

Allowed to assume
    The harness constructed ``MarketView`` from as-of-safe data at clock.now.
    The view contains books with staleness, own positions, and own fills.

Must never
    Import ``wxmm.data``, ``wxmm.venues``, ``wxmm.settlement``, or
    ``wxmm.backtest``. Contain a trading strategy, a fair-value model, or an
    order send. Receive a store handle, venue adapter, settlement object, or
    clock. Implementations belong in ``tests/`` (canary) or ``strategies/``
    (user) — both are import-linted.
"""

from __future__ import annotations

from wxmm.strategy.protocol import Strategy
from wxmm.strategy.view import BookView, FillView, MarketView, PositionView, ProposedOrder

__all__ = [
    "BookView",
    "FillView",
    "MarketView",
    "PositionView",
    "ProposedOrder",
    "Strategy",
]
