"""WXMM: weather-exchange market mechanics.

Trading and backtest infrastructure for Kalshi and Polymarket NYC daily-high
markets. This package does not replace ``ingestion/`` or ``analysis/``.

Allowed to assume
    Venue facts that live in ``knowledge/venue-facts.md`` and are encoded as
    explicit verified / unverified / tagged-assumption types.

Must never
    Invent venue fees, settlement rules, or station equivalence. Must never
    contain a fair-value model, a production strategy, or an order router.
    Must never treat Kalshi NYC and Polymarket NYC as the same underlying.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
