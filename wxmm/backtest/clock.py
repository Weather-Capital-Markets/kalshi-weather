"""Simulation clock. The only 'now' in a backtest."""

from __future__ import annotations

from datetime import datetime, timedelta

from wxmm.core.utc import require_utc


class SimClock:
    """Monotonic UTC clock. Cannot go backwards. Never reads the wall clock."""

    def __init__(self, start: datetime) -> None:
        self._now = require_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        ts_utc = require_utc(ts)
        if ts_utc < self._now:
            raise ValueError(
                f"clock cannot go backwards: {ts_utc.isoformat()} < {self._now.isoformat()}"
            )
        self._now = ts_utc

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("clock cannot go backwards")
        self._now = self._now + delta
