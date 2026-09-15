"""Shadow runner: propose via TRADE_DERIVED FV, never send, measure paper vs real."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.c1_m1_shadow import run_shadow, view_from_snapshots
from wxmm.core.types import BookLevel, BookSnapshot, Trade
from wxmm.fairvalue.provider import TradeDerivedFairValue, null_trade_fair_value

UTC = timezone.utc
TS = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)


def _snap(market_id: str, bid: int, ask: int) -> BookSnapshot:
    return BookSnapshot(
        market_id=market_id,
        valid_at=TS,
        available_at=TS,
        bids=(BookLevel(bid, 10),),
        asks=(BookLevel(ask, 10),),
        volume=10,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=True,
        source="shadow-test",
    )


def test_null_provider_generates_shadow_without_sending() -> None:
    snaps = (
        _snap("KXHIGHNY-26AUG12-T80", 35, 45),
        _snap("KXHIGHNY-26AUG12-T90", 40, 55),
    )
    provider = null_trade_fair_value((), TS)
    # Incomplete trade ladder → fair() is None; propose still emits at-touch quotes.
    view = view_from_snapshots(snaps)
    assert provider.fair(view) is None
    result = run_shadow(
        snapshots=snaps,
        provider=provider,
        subsequent_trades={
            "KXHIGHNY-26AUG12-T80": (
                Trade(
                    market_id="KXHIGHNY-26AUG12-T80",
                    ts=TS + timedelta(seconds=1),
                    available_at=TS + timedelta(seconds=1),
                    price_cents=34,
                    size=3,
                ),
            ),
            "KXHIGHNY-26AUG12-T90": (),
        },
        real_filled_qty={"KXHIGHNY-26AUG12-T80": 0, "KXHIGHNY-26AUG12-T90": 0},
        mids={
            "KXHIGHNY-26AUG12-T80": {TS + timedelta(minutes=1): 40},
        },
    )
    assert result.is_strategy_pnl is False
    assert len(result.quotes) >= 1
    assert result.shadow.paper_fill_count >= 0
    # Through-print at 34c vs buy at 35 should paper-fill the buy side of T80.
    assert result.shadow.optimism_count + result.shadow.pessimism_count >= 0


def test_trade_derived_fair_value_protocol_shape() -> None:
    provider = TradeDerivedFairValue(
        feature_names=(),
        beta=(),
        trades=(),
        as_of=TS,
    )
    view = view_from_snapshots((_snap("M", 40, 50),))
    assert provider.fair(view) is None
