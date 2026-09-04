"""Same config + snapshot + seed ⇒ identical canonical bytes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from wxmm.backtest.replay import MarketEvent, run
from wxmm.core.types import BookLevel, BookSnapshot, InMemoryAsOfStore, ReadContext
from wxmm.venues.base import get_venue

UTC = timezone.utc
PREREG = Path(__file__).resolve().parents[3] / "prereg"
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


class IdleStrategy:
    def on_event(self, ctx: ReadContext) -> list[object]:
        _ = ctx.clock.now()
        return []


def _book() -> BookSnapshot:
    return BookSnapshot(
        market_id="KXHIGHNY-26JUL04-T90",
        valid_at=TS,
        available_at=TS,
        bids=(BookLevel(40, 5),),
        asks=(BookLevel(42, 5),),
        volume=0,
        ask_size_known=True,
        reconstructed=True,
        staleness=timedelta(seconds=15),
        two_sided=True,
    )


def test_replay_is_byte_identical_for_same_config_snapshot_seed() -> None:
    config = yaml.safe_load((PREREG / "stage_b1_smoke.yaml").read_text())
    events = (
        MarketEvent(
            ts=TS,
            kind="book",
            market_id="KXHIGHNY-26JUL04-T90",
            payload=_book(),
            climate_day="2026-07-04",
        ),
    )
    venue = get_venue("kalshi", InMemoryAsOfStore())
    a = run(config=config, prereg_dir=PREREG, strategy=IdleStrategy(), venue=venue, events=events)
    b = run(config=config, prereg_dir=PREREG, strategy=IdleStrategy(), venue=venue, events=events)
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.seed == config["seed"]
    assert a.snapshot_id == b.snapshot_id
