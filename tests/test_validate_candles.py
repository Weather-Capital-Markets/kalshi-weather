"""Tests for the candle-emission validation checks."""

from __future__ import annotations

from analysis.spread_census import candle_fields
from analysis.validate_candles import (
    HISTORICAL_TIER,
    LIVE_TIER,
    emptying_events,
    gap_distribution,
    tier_of,
    volume_reconciliation,
)


def _candle(end_ts: int, bid: str | None, ask: str | None, volume: str = "0.00") -> dict:
    return candle_fields(
        {
            "end_period_ts": end_ts,
            "yes_bid": {"close": bid},
            "yes_ask": {"close": ask},
            "volume": volume,
        }
    )


def test_tier_is_read_from_the_capture_endpoint() -> None:
    assert tier_of("/historical/markets/HIGHNY-24AUG15-T83/candlesticks") == HISTORICAL_TIER
    assert tier_of("/series/KXHIGHNY/markets/KXHIGHNY-26AUG12-T90/candlesticks") == LIVE_TIER


def test_gap_distribution_dedupes_overlapping_chunk_edges() -> None:
    # Chunked fetches repeat the boundary candle; counting it would invent a 0 s gap.
    candles = [_candle(0, "0.40", "0.45"), _candle(60, "0.40", "0.45"), _candle(60, "0.40", "0.45")]
    stats = gap_distribution({"T": candles})
    assert stats["n_candles"] == 2
    assert stats["n_gaps"] == 1
    assert stats["modal_gap_sec"] == 60
    assert stats["share_gap_over_15min"] == 0.0
    assert stats["share_gap_over_one_period"] == 0.0


def test_gap_distribution_flags_sparse_emission() -> None:
    candles = [_candle(0, "0.40", "0.45"), _candle(3600, "0.40", "0.45")]
    stats = gap_distribution({"T": candles})
    assert stats["modal_gap_sec"] == 3600
    assert stats["share_gap_over_15min"] == 1.0


def test_volume_reconciliation_detects_a_dropped_trade_bearing_period() -> None:
    matching = _candle(0, "0.40", "0.45", volume="7.00")
    market_ok = {"ticker": "OK", "volume_fp": "7.00"}
    market_short = {"ticker": "SHORT", "volume_fp": "9.00"}
    frame = volume_reconciliation(
        markets=[market_ok, market_short],
        candles_by_tier={
            LIVE_TIER: {},
            HISTORICAL_TIER: {"OK": [matching], "SHORT": [matching]},
        },
    )
    by_ticker = frame.set_index("ticker")
    assert bool(by_ticker.loc["OK", "sum_matches"])
    assert not bool(by_ticker.loc["SHORT", "sum_matches"])
    assert by_ticker.loc["SHORT", "sum_difference"] == -2.0


def test_emptying_events_counts_two_sided_to_empty_transitions() -> None:
    # Carry-forward is only safe if the tier says when a book went away.
    candles = [
        _candle(0, "0.40", "0.45"),
        _candle(60, "0.0000", "1.0000"),
        _candle(120, "0.40", "0.45"),
    ]
    frame, samples = emptying_events({"T": candles})
    row = frame.iloc[0]
    assert int(row["n_empty_book_candles"]) == 1
    assert int(row["n_two_sided_to_empty"]) == 1
    assert samples["T"][0]["end_period_ts"] == 60


def test_emptying_events_reports_none_when_the_book_never_empties() -> None:
    frame, samples = emptying_events(
        {"T": [_candle(0, "0.40", "0.45"), _candle(60, "0.41", "0.44")]}
    )
    assert int(frame.iloc[0]["n_two_sided_to_empty"]) == 0
    assert samples == {}
