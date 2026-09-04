"""Strategy Protocol. Zero implementations in this module.

A strategy must not be able to reach the data store, venue adapters,
settlement, or the backtest clock. The harness builds ``MarketView`` from
as-of-safe data only.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from wxmm.strategy.view import MarketView, ProposedOrder


@runtime_checkable
class Strategy(Protocol):
    """Propose orders. The human sends them. This package never routes."""

    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        """Return proposed intents for this snapshot. May be empty."""
        ...
