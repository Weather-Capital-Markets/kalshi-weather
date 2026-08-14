"""Spread-census staleness and output tests."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from analysis.spread_census import (
    build_snapshot_table,
    candle_fields,
    is_stale,
    is_two_sided,
    last_candle_at_or_before,
    run,
    ticker_climate_date,
)
from ingestion.climate_day import climate_day_end
from ingestion.writer import RawJsonlWriter, utc_now_iso


def test_ticker_climate_date() -> None:
    assert ticker_climate_date("KXHIGHNY-26AUG13-T90") == "2026-08-13"


def test_staleness_guard() -> None:
    snapshot = 1_000_000
    fresh = {"end_period_ts": snapshot - 14 * 60}
    stale = {"end_period_ts": snapshot - 16 * 60}
    assert not is_stale(fresh, snapshot)
    assert is_stale(stale, snapshot)
    chosen = last_candle_at_or_before([fresh, stale], snapshot)
    assert chosen == fresh


def test_extreme_quotes_are_not_two_sided() -> None:
    # A6: bid 0.00 / ask 1.00 is an empty book, not a 99-cent spread.
    assert not is_two_sided(0.00, 1.00)
    assert not is_two_sided(0.00, 0.03)
    assert not is_two_sided(0.40, 1.00)
    assert is_two_sided(0.01, 0.99)
    assert is_two_sided(0.40, 0.45)


def test_probe_empty_book_candle_is_excluded_from_spread_stats() -> None:
    # Verbatim first candle from the 2026-08-14 --probe run.
    candle = candle_fields(
        {
            "end_period_ts": 1786456860,
            "open_interest_fp": "951.00",
            "price": {"close_dollars": "0.0100"},
            "volume_fp": "951.00",
            "yes_ask": {"close_dollars": "1.0000"},
            "yes_bid": {"close_dollars": "0.0000"},
        }
    )
    assert candle["bid_close"] == 0.0
    assert candle["ask_close"] == 1.0
    assert not is_two_sided(candle["bid_close"], candle["ask_close"])


def test_snapshot_table_flags_empty_book_without_spread(tmp_path: Path) -> None:
    climate_date = "2026-07-04"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot_ts = int((t_end - timedelta(hours=1)).timestamp())
    ticker = "KXHIGHNY-26JUL04-T90"
    candles = [
        candle_fields(
            {
                "end_period_ts": snapshot_ts - 60,
                "yes_bid": {"close_dollars": "0.0000"},
                "yes_ask": {"close_dollars": "1.0000"},
                "volume_fp": "0.00",
            }
        )
    ]
    snapshots, _stats = build_snapshot_table(
        markets=[{"ticker": ticker}],
        candles_by_ticker={ticker: candles},
        labels=pd.DataFrame(),
    )
    row = snapshots[snapshots["horizon_h"] == 1].iloc[0]
    assert bool(row["quote_valid"])
    assert not bool(row["two_sided"])
    assert bool(row["extreme_empty_book"])
    assert pd.isna(row["spread"])


def test_census_writes_csv_and_pngs(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    climate_date = "2026-07-04"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot = t_end - timedelta(hours=1)
    end_ts = int(snapshot.timestamp()) - 60
    ticker = "KXHIGHNY-26JUL04-T90"
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/markets",
        category="markets_history",
        key="KXHIGHNY_settled",
        http_status=200,
        latency_ms=1,
        payload={
            "markets": [
                {
                    "ticker": ticker,
                    "open_time": "2026-07-02T10:00:00Z",
                    "close_time": "2026-07-05T05:00:00Z",
                    "status": "settled",
                }
            ]
        },
    )
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/candlesticks",
        category="candlesticks",
        key=ticker,
        http_status=200,
        latency_ms=1,
        payload={
            "ticker": ticker,
            "candlesticks": [
                {
                    "end_period_ts": end_ts,
                    "yes_bid": {"close_dollars": "0.40"},
                    "yes_ask": {"close_dollars": "0.45"},
                    "volume_fp": "12.00",
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
        csv_writer = csv.DictWriter(handle, fieldnames=fields)
        csv_writer.writeheader()
        csv_writer.writerow(
            {
                "issuance_ts_utc": "2026-07-05T06:20:00Z",
                "climate_date": climate_date,
                "high_F": "94",
                "time_of_high_raw": "455 PM",
                "time_col_label": "(LST)",
                "is_same_day_intermediate": "False",
                "raw_pil": "CLINYC",
                "source_month": "2026-07",
            }
        )
    out_dir = tmp_path / "out"
    config = {"storage": {"raw_dir": str(raw_dir), "labels_csv": str(labels)}}
    assert run(config, out_dir) == 0
    assert (out_dir / "spread_census.csv").exists()
    for name in (
        "spread_vs_horizon.png",
        "spread_by_season.png",
        "brackets_per_day.png",
        "twosided_vs_horizon.png",
    ):
        assert (out_dir / name).exists()
    summary = pd.read_csv(out_dir / "spread_census.csv")
    assert "coverage" in summary.columns
    assert "two_sided_share" in summary.columns
    assert "tradeable_brackets_per_day_median" in summary.columns
    assert "extreme_empty_book_share" in summary.columns
