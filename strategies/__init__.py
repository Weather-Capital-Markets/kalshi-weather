"""User strategy modules. Import-linted: no store, venues, settlement, or clock.

Allowed to assume
    ``on_snapshot(MarketView)`` is the only callback. View is as-of-safe.

Must never
    Import ``wxmm.data``, ``wxmm.venues``, ``wxmm.settlement``, or
    ``wxmm.backtest``. Call ``store.get``. This package contains no production
    strategies (Stage B1).
"""
