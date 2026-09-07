"""Backtest engine.

Allowed to assume
    The simulation clock is the only 'now'. Fills are last-in-queue by
    default. Kalshi fees are exact given the rounding parameter. Coverage
    reports are mandatory. Config hashes must be pre-registered.

Must never
    Call ``datetime.now`` / ``time.time``. Auto-execute orders. Price
    Polymarket fills while unverified. Guess ask size. Unlock held-out data
    silently. Emit a result without a coverage report. Use async.
"""

from __future__ import annotations
