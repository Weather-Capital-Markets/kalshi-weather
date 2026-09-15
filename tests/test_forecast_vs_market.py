"""Tests for NBM forecast vs market comparison at T-24h."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from analysis.bracket_enumeration import ParsedStrike, parse_market_strike
from analysis.forecast_vs_market import (
    TAIL_CLAMP,
    TAIL_LINEAR,
    attach_normalized_mids,
    bracket_id,
    bracket_probabilities_for_markets,
    bracket_probability,
    build_comparison_table,
    cdf_at_temperature,
    k2_murphy_table,
    k2_window_split,
    select_unique_snapshot,
    settled_yes,
    summarize_comparison,
)
from ingestion.climate_day import climate_day_end
from ingestion.writer import RawJsonlWriter, utc_now_iso


def _modern_markets(prefix: str) -> list[dict]:
    return [
        {
            "ticker": f"{prefix}-T83",
            "strike_type": "less",
            "cap_strike": 83,
            "yes_sub_title": "82° or below",
            "open_time": "2022-12-13T10:00:00Z",
            "close_time": "2022-12-16T05:00:00Z",
        },
        {
            "ticker": f"{prefix}-B87.5",
            "strike_type": "between",
            "floor_strike": 87,
            "cap_strike": 88,
            "yes_sub_title": "87° to 88°",
            "open_time": "2022-12-13T10:00:00Z",
            "close_time": "2022-12-16T05:00:00Z",
        },
        {
            "ticker": f"{prefix}-T90",
            "strike_type": "greater",
            "floor_strike": 90,
            "yes_sub_title": "91° or above",
            "open_time": "2022-12-13T10:00:00Z",
            "close_time": "2022-12-16T05:00:00Z",
        },
    ]


def test_bracket_id_and_settlement() -> None:
    between = ParsedStrike("between", 87, 88, 2, "metadata")
    assert bracket_id(between) == "between_87_88"
    assert settled_yes(87, between) is True
    assert settled_yes(86, between) is False
    less = ParsedStrike("less", None, 83, None, "metadata")
    assert settled_yes(82, less) is True
    assert settled_yes(83, less) is False
    greater = ParsedStrike("greater", 90, None, None, "metadata")
    assert settled_yes(91, greater) is True
    assert settled_yes(90, greater) is False


def test_cdf_and_bracket_probability_monotone_ladder() -> None:
    levels = [10, 50, 90]
    values = [70.0, 75.0, 80.0]
    assert cdf_at_temperature(levels, values, 75.0) == pytest.approx(0.50, abs=0.01)
    strike = parse_market_strike({"strike_type": "between", "floor_strike": 74, "cap_strike": 76})
    assert strike is not None
    prob = bracket_probability(strike, levels, values)
    assert 0.0 < prob < 1.0


def test_bracket_probabilities_normalize_to_one() -> None:
    ladder = pd.DataFrame(
        {
            "percentile_level": [10, 50, 90],
            "value_f": [85.0, 87.5, 90.0],
        }
    )
    markets = _modern_markets("KXHIGHNY-22DEC15")
    probs = bracket_probabilities_for_markets(ladder, markets)
    assert len(probs) == 3
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(value >= 0.0 for value in probs.values())


def test_build_comparison_table_with_synthetic_raw(tmp_path: Path) -> None:
    climate_date = "2022-12-15"
    prefix = "KXHIGHNY-22DEC15"
    raw_dir = tmp_path / "raw"
    decoded_dir = tmp_path / "decoded"
    decoded_dir.mkdir()
    pd.DataFrame(
        {
            "percentile_level": [10, 50, 90],
            "value_f": [85.0, 87.5, 90.0],
            "forecast_hour": [42, 42, 42],
            "vintage_cycle_utc": ["2022-12-14T12:00:00+00:00"] * 3,
            "snapshot_margin_min_p90": [579.0, 579.0, 579.0],
        }
    ).to_parquet(decoded_dir / f"{climate_date}.parquet", index=False)

    writer = RawJsonlWriter(raw_dir)
    markets = _modern_markets(prefix)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/historical/markets",
        category="markets_history",
        key="evt",
        http_status=200,
        latency_ms=1,
        payload={"markets": markets},
    )
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot_ts = int((t_end - timedelta(hours=24)).timestamp())
    for market in markets:
        writer.write(
            ts_utc=utc_now_iso(),
            endpoint=f"/historical/markets/{market['ticker']}/candlesticks",
            category="candlesticks",
            key=market["ticker"],
            http_status=200,
            latency_ms=1,
            payload={
                "ticker": market["ticker"],
                "candlesticks": [
                    {
                        "end_period_ts": snapshot_ts,
                        "yes_bid": {"close": "0.20"},
                        "yes_ask": {"close": "0.30"},
                        "volume": "10",
                    }
                ],
            },
        )
    writer.close()

    labels = tmp_path / "clinyc.csv"
    fields = [
        "issuance_ts_utc",
        "climate_date",
        "high_F",
        "time_of_high_raw",
        "time_col_label",
        "is_same_day_intermediate",
        "raw_pil",
        "source_month",
    ]
    with labels.open("w", encoding="utf-8", newline="") as handle:
        writer_csv = csv.DictWriter(handle, fieldnames=fields)
        writer_csv.writeheader()
        writer_csv.writerow(
            {
                "issuance_ts_utc": "2022-12-16T06:20:00Z",
                "climate_date": climate_date,
                "high_F": "87",
                "time_of_high_raw": "315 PM",
                "time_col_label": "LST",
                "is_same_day_intermediate": "false",
                "raw_pil": "",
                "source_month": "202212",
            }
        )

    config = {
        "storage": {
            "raw_dir": str(raw_dir),
            "labels_csv": str(labels),
        },
        "nbm_archive": {
            "decoded_dir": str(decoded_dir),
            "start_date": climate_date,
            "end_date": climate_date,
            "sample_size": 1,
            "sample_seed": 1,
            "sub_era_split": "2024-05-15",
        },
        "forecast_vs_market": {
            "primary_band": [0.10, 0.90],
        },
    }
    table = build_comparison_table(config=config, climate_dates=[climate_date], horizon_h=24)
    assert len(table) == 3
    between = table[table["bracket_id"] == "between_87_88"].iloc[0]
    assert bool(between["settled_yes"]) is True
    assert between["market_mid_carryforward"] == pytest.approx(0.25)
    assert between["nbm_prob"] > 0.0
    summary = summarize_comparison(table)
    assert not summary.empty
    assert "market_brier_normalized" in summary.columns
    assert table["market_mid_normalized"].notna().all()
    day_sum = table.groupby("climate_date")["market_mid_normalized"].sum()
    assert day_sum.iloc[0] == pytest.approx(1.0)


def test_tail_clamp_puts_mass_below_p10_at_zero() -> None:
    levels = [10, 50, 90]
    values = [70.0, 75.0, 80.0]
    linear = cdf_at_temperature(levels, values, 67.5, tail=TAIL_LINEAR)
    clamp = cdf_at_temperature(levels, values, 67.5, tail=TAIL_CLAMP)
    assert linear > 0.0
    assert clamp == pytest.approx(0.0)


def test_select_unique_snapshot_raises_on_duplicates() -> None:
    snap = pd.DataFrame({"ticker": ["A", "A"], "mid_carryforward": [0.2, 0.3]})
    with pytest.raises(ValueError, match="duplicate snapshots"):
        select_unique_snapshot(snap, ticker="A", horizon_h=24)
    assert select_unique_snapshot(snap.iloc[0:0], ticker="A", horizon_h=24) is None


def test_k2_window_split_include_and_exclude() -> None:
    frame = pd.DataFrame(
        {
            "climate_date": ["2022-12-15", "2022-12-16"],
            "in_primary_band": [True, True],
            "settled_yes": [1.0, 0.0],
            "nbm_prob": [0.8, 0.2],
            "market_mid_carryforward": [0.7, 0.3],
            "market_mid_normalized": [0.7, 0.3],
            "edge_cf": [0.0, 0.0],
            "two_sided_carryforward": [True, True],
            "window_mismatch": [False, True],
        }
    )
    split = k2_window_split(frame)
    include = split[split["era"] == "include"].iloc[0]
    exclude = split[split["era"] == "exclude_mismatch"].iloc[0]
    assert include["n_obs"] == 2
    assert exclude["n_obs"] == 1
    murphy = k2_murphy_table(frame)
    assert set(murphy["model"]) >= {"nbm", "market_cf"}


def test_normalized_mids_leave_missing_alone() -> None:
    frame = pd.DataFrame(
        {
            "climate_date": ["2022-12-15", "2022-12-15"],
            "market_mid_carryforward": [0.40, None],
            "nbm_prob": [0.5, 0.5],
        }
    )
    out = attach_normalized_mids(frame)
    assert out.loc[0, "market_mid_normalized"] == pytest.approx(1.0)
    assert pd.isna(out.loc[1, "market_mid_normalized"])
