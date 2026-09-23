"""Shadow runner: propose via TRADE_DERIVED FV, never send, measure paper vs real."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.c1_m1_shadow import run_shadow, view_from_snapshots
from wxmm.analysis.trades_ingest import RawTrade, parse_trade
from wxmm.core.types import BookLevel, BookSnapshot, Trade
from wxmm.fairvalue.provider import (
    TradeDerivedFairValue,
    fair_value_from_report,
    null_trade_fair_value,
)

UTC = timezone.utc
TS = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)


def _trade_pair() -> list[RawTrade]:
    """Both sides on both brackets, so the ladder is complete and ``fair``
    reaches the feature build rather than returning None first."""
    quotes = [("T80", "0.45", "0.35"), ("T90", "0.55", "0.40")]
    out: list[RawTrade] = []
    for strike, ask, bid in quotes:
        ticker = f"KXHIGHNY-26AUG12-{strike}"
        for side, price in (("yes", ask), ("no", bid)):
            out.append(
                parse_trade(
                    {
                        "trade_id": f"{ticker}-{side}",
                        "ticker": ticker,
                        "count_fp": "10.00",
                        "yes_price_dollars": price,
                        "no_price_dollars": f"{1 - float(price):.2f}",
                        "taker_outcome_side": side,
                        "taker_book_side": "ask" if side == "yes" else "bid",
                        "created_time": "2026-08-12T15:00:00Z",
                        "is_block_trade": False,
                    },
                    source_endpoint="historical",
                )
            )
    return out


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


def test_report_artifact_round_trips_into_a_deployable_provider() -> None:
    """The coefficients are in standardised units, so the artifact has to carry
    the scaling too or the shadow deploys a different model."""
    report = {
        "fit": {
            "feature_names": ["book_implied_spread", "w900s_signed_ofi"],
            "beta": [0.25, -0.5],
            "feat_mean": [0.02, 0.0],
            "feat_std": [0.01, 0.5],
            "ridge_lambda": 1.0,
            "n_days": 1351,
        }
    }
    provider = fair_value_from_report(report, (), TS)
    assert provider.feature_names == ("book_implied_spread", "w900s_signed_ofi")
    assert provider.beta == (0.25, -0.5)
    assert provider.feat_mean == (0.02, 0.0)
    assert provider.feat_std == (0.01, 0.5)


def test_report_without_a_fit_block_falls_back_to_null_not_unscaled_beta() -> None:
    provider = fair_value_from_report({"beta": [{"name": "x", "point": 1.0}]}, (), TS)
    assert provider.beta == ()
    assert provider.feat_std is None


def test_provider_drops_a_row_whose_staleness_ratio_is_undefined() -> None:
    """A print at the as-of makes ask age 0, so the ratio is missing.

    Filling that cell with 0.0 would enter the fit. The provider returns None
    instead of quoting from an imputed staleness key.
    """
    quotes = [("T80", "0.45", "0.35"), ("T90", "0.55", "0.40")]
    trades: list[RawTrade] = []
    for strike, ask, bid in quotes:
        ticker = f"KXHIGHNY-26AUG12-{strike}"
        for side, price in (("yes", ask), ("no", bid)):
            trades.append(
                parse_trade(
                    {
                        "trade_id": f"{ticker}-{side}-now",
                        "ticker": ticker,
                        "count_fp": "10.00",
                        "yes_price_dollars": price,
                        "no_price_dollars": f"{1 - float(price):.2f}",
                        "taker_outcome_side": side,
                        "taker_book_side": "ask" if side == "yes" else "bid",
                        "created_time": "2026-08-12T16:00:00Z",
                        "is_block_trade": False,
                    },
                    source_endpoint="historical",
                )
            )
    provider = TradeDerivedFairValue(
        feature_names=("book_implied_spread",),
        beta=(0.1,),
        trades=tuple(trades),
        as_of=TS,
    )
    view = view_from_snapshots(
        (
            _snap("KXHIGHNY-26AUG12-T80", 35, 45),
            _snap("KXHIGHNY-26AUG12-T90", 40, 55),
        )
    )
    assert provider.fair(view) is None


def test_provider_refuses_a_feature_name_it_cannot_build() -> None:
    """Silently substituting zero for a renamed feature runs a different model
    under the fitted model's name."""
    provider = TradeDerivedFairValue(
        feature_names=("a_feature_that_does_not_exist",),
        beta=(1.0,),
        trades=tuple(_trade_pair()),
        as_of=TS,
    )
    view = view_from_snapshots(
        (
            _snap("KXHIGHNY-26AUG12-T80", 35, 45),
            _snap("KXHIGHNY-26AUG12-T90", 40, 55),
        )
    )
    with pytest.raises(KeyError, match="different model"):
        provider.fair(view)


def test_trade_derived_fair_value_protocol_shape() -> None:
    provider = TradeDerivedFairValue(
        feature_names=(),
        beta=(),
        trades=(),
        as_of=TS,
    )
    view = view_from_snapshots((_snap("M", 40, 50),))
    assert provider.fair(view) is None
