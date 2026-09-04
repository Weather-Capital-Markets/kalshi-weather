"""Operator tools: console, journal, reconcile.

Allowed to assume
    Humans send orders. The system proposes. Both venues may be shown at once
    but never netted.

Must never
    Route an order. Hide unverified-fact blockers. Report residual basis as
    zero on a cross-venue hedge. Call wall-clock from backtest-reachable
    paths (ops is live; inject a clock).
"""

from __future__ import annotations
