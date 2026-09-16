"""Replay and harness refuse an unbound AsOfStore."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from wxmm.backtest.replay import run
from wxmm.core.types import FrozenClock, InMemoryAsOfStore
from wxmm.core.view import build_market_view
from wxmm.strategy.view import MarketView, ProposedOrder
from wxmm.venues.base import get_venue

UTC = timezone.utc
PREREG = Path(__file__).resolve().parents[3] / "prereg"


class Idle:
    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        _ = view.books
        return []


def test_replay_refuses_unbound_store() -> None:
    config = yaml.safe_load((PREREG / "stage_b1_smoke.yaml").read_text())
    venue = get_venue("kalshi", InMemoryAsOfStore())
    with pytest.raises(TypeError, match="unbound store refused"):
        run(
            config=config,
            prereg_dir=PREREG,
            strategy=Idle(),
            venue=venue,
            events=(),
            store=InMemoryAsOfStore(),
        )


def test_harness_refuses_unbound_store() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 18, 0, tzinfo=UTC))
    with pytest.raises(TypeError, match="unbound store refused"):
        build_market_view(
            store=InMemoryAsOfStore(),  # type: ignore[arg-type]
            clock=clock,
            book_keys=(),
        )
