"""AST + runtime wall-clock guards."""

from __future__ import annotations

import datetime as datetime_module
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wxmm.backtest.clock import SimClock
from wxmm.backtest.guards import assert_no_wall_clock_source, wall_clock_guard
from wxmm.core.errors import WallClockError

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]


def test_backtest_reachable_source_has_no_datetime_now() -> None:
    hits = assert_no_wall_clock_source(ROOT)
    assert hits == []


def test_runtime_guard_raises_on_datetime_now() -> None:
    with wall_clock_guard():
        with pytest.raises(WallClockError):
            datetime_module.datetime.now(UTC)
        with pytest.raises(WallClockError):
            time.time()


def test_sim_clock_is_monotonic() -> None:
    clock = SimClock(datetime(2026, 7, 4, 12, 0, tzinfo=UTC))
    clock.advance_to(datetime(2026, 7, 4, 13, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(datetime(2026, 7, 4, 12, 0, tzinfo=UTC))
