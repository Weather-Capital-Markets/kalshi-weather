"""Live operating path: state, feeds, rate limits, fail-closed credentials.

Allowed to assume
    Async is allowed in this package only. ``LiveState`` is the live
    ``BookSource``. Replay uses the same ``BookUpdate`` records.

Must never
    Import ``wxmm.backtest``. Auto-send orders. Read the environment except
    via ``credentials.py``. Build a ``MarketView`` here — that lives in
    ``wxmm.core.view``.
"""

from __future__ import annotations
