"""Idle example. Import-linted: no store, venues, settlement, or clock."""

from __future__ import annotations

from wxmm.strategy.view import MarketView, ProposedOrder


class Idle:
    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        _ = view.books
        return []
