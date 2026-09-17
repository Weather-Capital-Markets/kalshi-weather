"""Decide package: proposals, never orders.

Allowed to assume
    A ``MarketView`` is already as-of-safe. Fair value may be absent.

Must never
    Import ``wxmm.execute`` or ``wxmm.live``. Send an order. Assume a fair
    value exists. Import ``wxmm.backtest``.
"""

from __future__ import annotations
