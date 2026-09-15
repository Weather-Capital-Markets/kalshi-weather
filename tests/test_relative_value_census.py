"""Unit tests for S2 relative-value census helpers (no live data)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.relative_value_census import (
    BAND_HI,
    BAND_LO,
    COVERAGE_COLUMNS,
    N_BRACKETS,
    NO_PAYOUT,
    BasketSnapshot,
    LegQuote,
    assemble_basket,
    basket_sums,
    collateral_arithmetic,
    complete_book_filter,
    complete_quotes,
    coverage_row,
    penny_excluded_arithmetic,
    quadratic_taker_fee,
    six_bracket_universe,
    size_constrained_slack,
    taker_adjusted_sums,
    violation_run,
)
from ingestion.climate_day import climate_day_end


def _leg(
    *,
    ticker: str = "HIGHNY-24AUG15-T80",
    bid: float | None = 0.10,
    ask: float | None = 0.12,
    no_ask: float | None = 0.90,
    no_ask_source: str = "implied",
    bid_size: float | None = None,
    ask_size: float | None = None,
    no_ask_size: float | None = None,
    two_sided: bool = True,
    mid: float | None = 0.11,
    in_band: bool | None = None,
) -> LegQuote:
    if in_band is None:
        in_band = mid is not None and BAND_LO <= mid <= BAND_HI
    return LegQuote(
        ticker=ticker,
        bid=bid,
        ask=ask,
        no_bid=None if ask is None else 1.0 - ask,
        no_ask=no_ask,
        no_ask_source=no_ask_source,
        bid_size=bid_size,
        ask_size=ask_size,
        no_ask_size=no_ask_size,
        end_period_ts=1,
        two_sided=two_sided,
        two_sided_strict15=two_sided,
        mid=mid,
        in_band=in_band,
        one_cent=bool(bid is not None and bid <= 0.01),
    )


def _complete_basket(legs: tuple[LegQuote, ...], *, in_window: bool = True) -> BasketSnapshot:
    n_two = sum(1 for leg in legs if leg.two_sided)
    complete = in_window and complete_book_filter([leg.two_sided for leg in legs])
    return BasketSnapshot(
        climate_date="2024-08-15",
        horizon_h=24,
        season="JJA",
        snapshot_ts=1,
        in_window=in_window,
        legs=legs,
        n_two_sided=n_two,
        n_two_sided_strict15=n_two,
        n_in_band=sum(1 for leg in legs if leg.in_band),
        n_outside_band=sum(1 for leg in legs if leg.two_sided and not leg.in_band),
        n_one_cent=sum(1 for leg in legs if leg.one_cent),
        n_explicit_no_ask=sum(1 for leg in legs if leg.no_ask_source == "explicit"),
        n_legs_with_ask_size=sum(1 for leg in legs if leg.ask_size is not None),
        n_legs_with_bid_size=sum(1 for leg in legs if leg.bid_size is not None),
        max_quote_age_sec=0.0,
        complete_unconditional=complete_book_filter([leg.two_sided for leg in legs]),
        complete_book=complete,
        complete_strict15=complete,
        all_in_band=complete and sum(1 for leg in legs if leg.in_band) == N_BRACKETS,
    )


def test_basket_sums_known_buy_and_sell_violation() -> None:
    asks = [0.10, 0.15, 0.20, 0.18, 0.12, 0.20]
    bids = [0.20, 0.18, 0.16, 0.17, 0.19, 0.14]
    no_asks = [0.80, 0.82, 0.84, 0.83, 0.81, 0.80]
    assert sum(asks) == pytest.approx(0.95)
    assert sum(bids) == pytest.approx(1.04)
    assert sum(no_asks) == pytest.approx(4.90)
    out = basket_sums(asks, bids, no_asks)
    assert out["slack_ask"] == pytest.approx(0.05)
    assert out["slack_bid"] == pytest.approx(0.04)
    assert out["slack_no"] == pytest.approx(0.10)
    assert out["slack_no"] == pytest.approx(NO_PAYOUT - 4.90)


def test_implied_no_ask_matches_gross_yes_sell() -> None:
    bids = [0.20] * 6
    asks = [0.21] * 6
    no_asks = [1.0 - b for b in bids]
    out = basket_sums(asks, bids, no_asks)
    assert out["sum_no_ask"] == pytest.approx(6.0 - out["sum_bid"])
    assert out["slack_no"] == pytest.approx(out["slack_bid"])


def test_complete_book_filter_requires_every_leg() -> None:
    assert complete_book_filter([True] * 6)
    assert not complete_book_filter([True] * 5 + [False])
    assert not complete_book_filter([True] * 5)


def test_partial_book_has_no_executable_quotes() -> None:
    legs = tuple(_leg(ticker=f"HIGHNY-24AUG15-T8{i}", two_sided=i < 5) for i in range(6))
    legs = legs[:-1] + (
        _leg(
            ticker="HIGHNY-24AUG15-T85", bid=0.0, ask=1.0, two_sided=False, mid=None, in_band=False
        ),
    )
    basket = _complete_basket(legs)
    assert not basket.complete_book
    assert complete_quotes(basket) is None
    row = coverage_row(basket)
    assert "sum_ask" not in row
    assert "slack_ask" not in row
    assert set(row) <= set(COVERAGE_COLUMNS)


def test_coverage_columns_exclude_violation_magnitudes() -> None:
    forbidden = {
        "sum_ask",
        "sum_bid",
        "sum_no_ask",
        "slack_ask",
        "slack_bid",
        "slack_no",
        "buy_violation",
        "sell_violation",
        "roc_buy",
        "penny_excluded_buy_violation",
        "penny_excluded_sell_violation",
        "slack_from_penny_leg",
    }
    assert forbidden.isdisjoint(COVERAGE_COLUMNS)


def test_collateral_buy_and_gross_sell() -> None:
    cap = collateral_arithmetic(sum_ask=0.95, sum_bid=1.04, sum_no_ask=4.90)
    assert cap["capital_buy"] == pytest.approx(0.95)
    assert cap["roc_buy"] == pytest.approx(0.05 / 0.95)
    assert cap["capital_sell_gross"] == pytest.approx(4.96)
    assert cap["roc_sell_gross"] == pytest.approx(0.04 / 4.96)
    assert cap["capital_sell_netted_1"] == pytest.approx(1.0)
    assert cap["roc_sell_netted_1"] == pytest.approx(0.04)
    assert cap["capital_sell_netted_offset"] == pytest.approx(0.0)
    assert cap["roc_sell_netted_offset"] is None
    assert cap["capital_buy_no"] == pytest.approx(4.90)
    assert cap["roc_buy_no"] == pytest.approx(0.10 / 4.90)


def test_taker_fee_raises_buy_cost() -> None:
    asks = [0.10, 0.15, 0.20, 0.18, 0.12, 0.20]
    bids = [0.09, 0.14, 0.19, 0.17, 0.11, 0.19]
    no_asks = [0.90] * 6
    raw = basket_sums(asks, bids, no_asks)
    fee = taker_adjusted_sums(asks, bids, no_asks)
    assert fee["sum_ask_after_fee"] > raw["sum_ask"]
    assert fee["sum_bid_after_fee"] < raw["sum_bid"]
    assert fee["slack_ask_after_fee"] < raw["slack_ask"]
    assert quadratic_taker_fee(0.50) == pytest.approx(0.0175)


def test_size_constrained_na_without_depth() -> None:
    legs = tuple(_leg(ticker=f"HIGHNY-24AUG15-T8{i}") for i in range(6))
    basket = _complete_basket(legs)
    sized = size_constrained_slack(basket, slack_ask=0.05, slack_bid=0.04, slack_no=0.10)
    assert sized["slack_ask_1lot"] == pytest.approx(0.05)
    assert sized["slack_ask_5lot"] is None
    assert sized["slack_ask_10lot"] is None
    assert sized["slack_ask_25lot"] is None


def test_size_constrained_scales_when_every_leg_has_size() -> None:
    legs = tuple(
        _leg(ticker=f"HIGHNY-24AUG15-T8{i}", ask_size=10.0, bid_size=10.0, no_ask_size=10.0)
        for i in range(6)
    )
    basket = _complete_basket(legs)
    sized = size_constrained_slack(basket, slack_ask=0.05, slack_bid=0.04, slack_no=0.10)
    assert sized["slack_ask_5lot"] == pytest.approx(0.25)
    assert sized["slack_ask_10lot"] == pytest.approx(0.50)
    assert sized["slack_ask_25lot"] is None


def test_violation_run_counts_candles_and_standing_duration() -> None:
    events = [(100, True), (160, True), (220, False)]
    n_events, duration = violation_run(events, snapshot_ts=130, close_ts=500)
    assert n_events == 2
    assert duration == pytest.approx(120.0)


def test_violation_run_none_when_snapshot_not_violating() -> None:
    events = [(100, False), (160, True)]
    n_events, duration = violation_run(events, snapshot_ts=120, close_ts=500)
    assert n_events is None
    assert duration is None


def test_six_bracket_universe_keeps_exactly_six_after_start() -> None:
    markets = [{"ticker": f"HIGHNY-24AUG15-T8{i}"} for i in range(6)] + [
        {"ticker": "HIGHNY-21AUG15-T80"},
        {"ticker": "HIGHNY-24AUG16-T80"},
        {"ticker": "HIGHNY-24AUG16-T81"},
    ]
    six, other = six_bracket_universe(markets)
    assert "2024-08-15" in six
    assert len(six["2024-08-15"]) == 6
    assert "2021-08-15" not in six
    assert other.get("2024-08-16") == 2


def test_last_indexed_bisect_matches_scan() -> None:
    from analysis.relative_value_census import build_candle_index, last_indexed
    from analysis.spread_census import last_candle_at_or_before

    candles = [
        {"end_period_ts": 100, "bid_close": 0.1},
        {"end_period_ts": 200, "bid_close": 0.2},
        {"end_period_ts": 300, "bid_close": 0.3},
    ]
    index = build_candle_index({"T": candles})
    for ts in (50, 100, 150, 200, 350):
        assert last_indexed(index, "T", ts) == last_candle_at_or_before(candles, ts)


def test_assemble_basket_complete_and_in_band() -> None:
    climate_date = "2024-08-15"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot = t_end - timedelta(hours=24)
    snapshot_ts = int(snapshot.timestamp())
    markets = []
    candles: dict[str, list] = {}
    for i in range(6):
        ticker = f"HIGHNY-24AUG15-T8{i}"
        markets.append(
            {
                "ticker": ticker,
                "open_time": (snapshot - timedelta(hours=20))
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close_time": (snapshot + timedelta(hours=20))
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        candles[ticker] = [
            {
                "end_period_ts": snapshot_ts - 60,
                "bid_close": 0.20,
                "ask_close": 0.22,
                "no_bid_close": None,
                "no_ask_close": None,
                "bid_size": None,
                "ask_size": None,
                "no_ask_size": None,
                "volume": 0.0,
            }
        ]
    basket = assemble_basket(
        climate_date=climate_date,
        horizon_h=24,
        markets=markets,
        candles_by_ticker=candles,
    )
    assert basket.complete_book
    assert basket.all_in_band
    quotes = complete_quotes(basket)
    assert quotes is not None
    sums = basket_sums(*quotes)
    assert sums["sum_ask"] == pytest.approx(1.32)
    assert sums["sum_no_ask"] == pytest.approx(4.80)
    assert basket.n_explicit_no_ask == 0


def test_penny_excluded_sell_disappears_when_99c_tail_dropped() -> None:
    asks = [0.20, 0.20, 0.20, 0.20, 0.20, 0.99]
    bids = [0.18, 0.18, 0.18, 0.18, 0.18, 0.98]
    orig = basket_sums(asks, bids, [1.0 - b for b in bids])
    assert orig["slack_bid"] == pytest.approx(0.88)
    assert orig["slack_ask"] < 0.0
    out = penny_excluded_arithmetic(asks, bids, orig_buy=False, orig_sell=True)
    assert out["n_penny_legs"] == 1
    assert out["n_non_penny_legs"] == 5
    assert out["sum_bid_excl_penny"] == pytest.approx(0.90)
    assert out["penny_excluded_sell_violation"] is False
    assert out["slack_from_penny_leg"] is True


def test_penny_excluded_sell_survives_when_all_legs_in_band() -> None:
    asks = [0.20] * 6
    bids = [0.18] * 6
    orig = basket_sums(asks, bids, [1.0 - b for b in bids])
    assert orig["slack_bid"] == pytest.approx(0.08)
    out = penny_excluded_arithmetic(asks, bids, orig_buy=False, orig_sell=True)
    assert out["n_penny_legs"] == 0
    assert out["n_non_penny_legs"] == 6
    assert out["penny_excluded_sell_violation"] is True
    assert out["slack_from_penny_leg"] is False


def test_penny_excluded_empty_remainder_is_from_penny() -> None:
    asks = [0.99] * 6
    bids = [0.01] * 6
    out = penny_excluded_arithmetic(asks, bids, orig_buy=False, orig_sell=False)
    assert out["n_non_penny_legs"] == 0
    assert out["penny_excluded_buy_violation"] is False
    assert out["penny_excluded_sell_violation"] is False
    assert out["slack_from_penny_leg"] is False
    out_viol = penny_excluded_arithmetic(asks, bids, orig_buy=True, orig_sell=False)
    assert out_viol["slack_from_penny_leg"] is True
