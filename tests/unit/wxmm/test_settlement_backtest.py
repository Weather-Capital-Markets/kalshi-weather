"""Held-to-expiry settlement fee is unverified; Polymarket revision flips are reported."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from wxmm.backtest.replay import MarketEvent, run
from wxmm.backtest.settle import (
    SettlementFeePolicy,
    hold_to_expiry_pnl,
    polymarket_resolution_path,
)
from wxmm.core.errors import UnverifiedSettlementFee
from wxmm.core.types import BookLevel, BookSnapshot, InMemoryAsOfStore, Trade
from wxmm.settlement.rules import Observation
from wxmm.strategy.view import MarketView, ProposedOrder
from wxmm.venues.base import get_venue

UTC = timezone.utc
PREREG = Path(__file__).resolve().parents[3] / "prereg"


def _pm_observations() -> tuple[Observation, ...]:
    climate = date(2025, 7, 4)
    return (
        Observation(
            station="KLGA",
            climate_day=climate,
            high_f=88,
            valid_at=datetime(2025, 7, 5, 3, 0, tzinfo=UTC),
            available_at=datetime(2025, 7, 5, 3, 0, tzinfo=UTC),
            source="WU_DAILY",
            is_full_day=True,
            is_revision=False,
        ),
        Observation(
            station="KLGA",
            climate_day=climate,
            high_f=89,
            valid_at=datetime(2025, 7, 5, 13, 30, tzinfo=UTC),
            available_at=datetime(2025, 7, 5, 13, 30, tzinfo=UTC),
            source="WU_DAILY",
            is_full_day=True,
            is_revision=True,
        ),
        Observation(
            station="KLGA",
            climate_day=date(2025, 7, 5),
            high_f=91,
            valid_at=datetime(2025, 7, 5, 16, 0, tzinfo=UTC),
            available_at=datetime(2025, 7, 5, 16, 0, tzinfo=UTC),
            source="WU_DAILY",
            is_full_day=False,
            is_revision=False,
        ),
    )


def test_unverified_settlement_fee_raises_on_hold_to_expiry_pnl() -> None:
    with pytest.raises(UnverifiedSettlementFee, match="hold-to-expiry"):
        hold_to_expiry_pnl(
            quantity=1,
            entry_cents=40,
            settle_cents=100,
            policy=SettlementFeePolicy(),
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-T90",
        )


def test_polymarket_revision_flip_day_is_reported() -> None:
    climate = date(2025, 7, 4)
    path = polymarket_resolution_path(
        climate,
        _pm_observations(),
        as_of_final=datetime(2025, 7, 6, 16, 0, tzinfo=UTC),
    )
    assert path.initial_high_f == 88
    assert path.final_high_f == 89
    assert path.flipped is True


class _Idle:
    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        _ = view
        return []


class _FillOnce:
    def __init__(self) -> None:
        self._sent = False

    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        if self._sent or not view.books:
            return []
        self._sent = True
        book = view.books[0]
        return [
            ProposedOrder(
                venue="kalshi",
                market=book.market_id,
                side="buy",
                price=40,
                size=1,
                rationale="fill-once for settlement fee canary",
            )
        ]


def test_replay_reports_polymarket_revision_flip_day() -> None:
    config = yaml.safe_load((PREREG / "stage_b1_smoke.yaml").read_text())
    ts = datetime(2025, 7, 4, 16, 0, tzinfo=UTC)
    book = BookSnapshot(
        market_id="nyc-daily-weather",
        valid_at=ts,
        available_at=ts,
        bids=(BookLevel(40, 5),),
        asks=(BookLevel(42, 5),),
        volume=0,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
    )
    events = (
        MarketEvent(
            ts=ts,
            kind="book",
            market_id="nyc-daily-weather",
            payload=book,
            climate_day="2025-07-04",
        ),
        MarketEvent(
            ts=datetime(2025, 7, 6, 16, 0, tzinfo=UTC),
            kind="clock",
            market_id="nyc-daily-weather",
            payload=None,
            climate_day="2025-07-04",
        ),
    )
    result = run(
        config=config,
        prereg_dir=PREREG,
        strategy=_Idle(),
        venue=get_venue("polymarket", InMemoryAsOfStore()),
        events=events,
        observations=_pm_observations(),
    )
    flips = result.extra["polymarket_revision_flips"]
    assert flips
    assert flips[0]["climate_day"] == "2025-07-04"
    assert flips[0]["initial_high_f"] == 88
    assert flips[0]["final_high_f"] == 89


def test_replay_unverified_settlement_fee_raises_on_hold_to_expiry() -> None:
    config = yaml.safe_load((PREREG / "stage_b1_smoke.yaml").read_text())
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
    market_id = "KXHIGHNY-26JUL04-T90"
    book = BookSnapshot(
        market_id=market_id,
        valid_at=ts,
        available_at=ts,
        bids=(BookLevel(39, 10),),
        asks=(BookLevel(40, 10),),
        volume=0,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
    )
    events = (
        MarketEvent(
            ts=ts,
            kind="book",
            market_id=market_id,
            payload=book,
            climate_day="2026-07-04",
        ),
    )
    trades = {
        market_id: (
            Trade(market_id=market_id, ts=ts, available_at=ts, price_cents=39, size=1),
        )
    }
    with pytest.raises(UnverifiedSettlementFee, match="hold-to-expiry"):
        run(
            config=config,
            prereg_dir=PREREG,
            strategy=_FillOnce(),
            venue=get_venue("kalshi", InMemoryAsOfStore()),
            events=events,
            trades_by_market=trades,
            observations=(),
        )
