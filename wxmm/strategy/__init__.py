"""Strategy Protocol only. Zero production implementations.

Allowed to assume
    The backtest clock is the only 'now'. Reads go through ``get(key, as_of)``.

Must never
    Contain a trading strategy, a fair-value model, or an order send.
    Implementations belong in ``tests/`` (canary/cheats) only.
"""

from __future__ import annotations

from wxmm.strategy.protocol import Strategy

__all__ = ["Strategy"]
