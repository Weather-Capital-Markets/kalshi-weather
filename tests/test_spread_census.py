"""Spread-census staleness and output tests."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from analysis.spread_census import (
    HORIZONS_H,
    build_snapshot_table,
    candle_fields,
    era_of,
    is_stale,
    is_two_sided,
    last_candle_at_or_before,
    run,
    summarize,
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


def test_horizon_grid_includes_t36_and_keeps_t48() -> None:
    # A7: T-48h predates market open almost everywhere but its empty column is
    # the evidence; T-36h is the earliest horizon with real books.
    assert 36 in HORIZONS_H
    assert 48 in HORIZONS_H
    assert HORIZONS_H == tuple(sorted(HORIZONS_H, reverse=True))


def test_era_split_keeps_2021_separate() -> None:
    assert era_of("2021-08-05") == "2021_early"
    assert era_of("2021-12-31") == "2021_early"
    assert era_of("2022-01-01") == "2022_plus"
    assert era_of("2026-08-13") == "2022_plus"


def test_historical_tier_flat_field_names_are_read() -> None:
    # Verbatim first candle from --probe --ticker HIGHNY-24AUG15-T83 (2026-08-14):
    # the historical tier uses flat close/volume, not *_dollars/volume_fp.
    fields = candle_fields(
        {
            "end_period_ts": 1723644060,
            "open_interest": "0.00",
            "price": {"close": None},
            "volume": "0.00",
            "yes_ask": {"close": "0.9800"},
            "yes_bid": {"close": "0.0100"},
        }
    )
    assert fields["bid_close"] == 0.01
    assert fields["ask_close"] == 0.98
    assert fields["volume"] == 0.0
    assert is_two_sided(fields["bid_close"], fields["ask_close"])


def test_sparse_candles_split_stale_from_carryforward(tmp_path: Path) -> None:
    # Historical candles are emitted on change, so a 30-minute-old quote is the
    # live book. The 15-minute rule drops it; carry-forward keeps it.
    climate_date = "2024-08-15"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot_ts = int((t_end - timedelta(hours=24)).timestamp())
    ticker = "HIGHNY-24AUG15-T83"
    candles = [
        candle_fields(
            {
                "end_period_ts": snapshot_ts - 30 * 60,
                "yes_bid": {"close": "0.0100"},
                "yes_ask": {"close": "0.9800"},
                "volume": "0.00",
            }
        )
    ]
    snapshots, stats = build_snapshot_table(
        markets=[
            {
                "ticker": ticker,
                "open_time": "2024-08-14T14:00:00Z",
                "close_time": "2024-08-16T03:59:00Z",
            }
        ],
        candles_by_ticker={ticker: candles},
        labels=pd.DataFrame(),
    )
    row = snapshots[snapshots["horizon_h"] == 24].iloc[0]
    assert not bool(row["quote_valid"])
    assert bool(row["stale"])
    assert bool(row["quote_present"])
    assert bool(row["two_sided_carryforward"])
    assert row["quote_age_sec"] == 30 * 60
    assert stats["stale_discards"] >= 1


def test_snapshot_outside_trading_window_is_flagged() -> None:
    # 2024-era markets closed 03:59Z, an hour before the climate-day end, so the
    # T-1h snapshot is structurally outside the window rather than unquoted.
    climate_date = "2024-08-15"
    ticker = "HIGHNY-24AUG15-T83"
    snapshots, _stats = build_snapshot_table(
        markets=[
            {
                "ticker": ticker,
                "open_time": "2024-08-14T14:00:00Z",
                "close_time": "2024-08-16T03:59:00Z",
            }
        ],
        candles_by_ticker={ticker: []},
        labels=pd.DataFrame(),
    )
    by_horizon = snapshots.set_index("horizon_h")
    assert not bool(by_horizon.loc[1, "in_trading_window"])
    assert not bool(by_horizon.loc[48, "in_trading_window"])
    assert bool(by_horizon.loc[24, "in_trading_window"])
    assert bool(by_horizon.loc[36, "in_trading_window"])
    assert climate_date == "2024-08-15"


def test_post_close_carried_quote_is_excluded_from_primary_spread_stats() -> None:
    # A 2024-era market shut at 03:59Z, an hour before the climate-day end. Its
    # last quote carries forward to T-1h, but the market was not tradeable then,
    # so the primary statistic must not count it.
    ticker = "HIGHNY-24AUG15-T83"
    t_end = climate_day_end(datetime.fromisoformat("2024-08-15").date())
    last_quote_ts = int((t_end - timedelta(hours=4)).timestamp())
    candles = [
        candle_fields(
            {
                "end_period_ts": last_quote_ts,
                "yes_bid": {"close": "0.4000"},
                "yes_ask": {"close": "0.4500"},
                "volume": "5.00",
            }
        )
    ]
    snapshots, _stats = build_snapshot_table(
        markets=[
            {
                "ticker": ticker,
                "open_time": "2024-08-14T14:00:00Z",
                "close_time": "2024-08-16T03:59:00Z",
            }
        ],
        candles_by_ticker={ticker: candles},
        labels=pd.DataFrame(),
    )
    row = snapshots[snapshots["horizon_h"] == 1].iloc[0]
    assert bool(row["two_sided_carryforward"])
    assert not bool(row["in_trading_window"])

    summary = summarize(snapshots)
    at_t1 = summary[(summary["horizon_h"] == 1) & (summary["band"] == "20_80")]
    assert int(at_t1["n_spread_obs_carryforward"].sum()) == 0
    assert int(at_t1["n_spread_obs_strict15"].sum()) == 0
    assert float(at_t1["outside_trading_window_share"].max()) == 1.0
    # T-3h is inside the window, so the same carried quote does count there.
    at_t3 = summary[(summary["horizon_h"] == 3) & (summary["band"] == "20_80")]
    assert int(at_t3["n_spread_obs_carryforward"].sum()) == 1


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
    # The pre-registration names these columns, so a rename is a protocol change.
    for column in (
        "coverage",
        "coverage_carryforward",
        "outside_trading_window_share",
        "extreme_empty_book_share",
        "quote_age_median_min",
        "quote_age_p90_min",
        "era",
    ):
        assert column in summary.columns
    for stem in (
        "two_sided_share",
        "two_sided_share_given_quote",
        "coverage_given_open",
        "tradeable_brackets_per_day_median",
        "median_spread",
        "p25",
        "p75",
        "p90",
        "n_spread_obs",
    ):
        assert f"{stem}_carryforward" in summary.columns
        assert f"{stem}_strict15" in summary.columns
        # An unsuffixed survivor would leave the reader guessing which rule it used.
        assert stem not in summary.columns
    assert set(summary["horizon_h"]) == set(HORIZONS_H)


def test_run_wires_volume_reconciliation_into_exclnoreconcile_columns(tmp_path: Path) -> None:
    """run() must load mismatch tickers and pass them into summarize().

    A wrong keyword to volume_mismatch_tickers() makes this fail before any CSV
    is written; a silent no-op would show carryforward == exclnoreconcile here.
    """
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    climate_date = "2026-07-04"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    snapshot_ts = int((t_end - timedelta(hours=24)).timestamp()) - 60

    ok_ticker = "KXHIGHNY-26JUL04-T90"
    bad_ticker = "KXHIGHNY-26JUL04-T91"
    markets = [
        {
            "ticker": ok_ticker,
            "open_time": "2026-07-02T10:00:00Z",
            "close_time": "2026-07-05T05:00:00Z",
            "status": "settled",
            "volume_fp": "12.00",
        },
        {
            "ticker": bad_ticker,
            "open_time": "2026-07-02T10:00:00Z",
            "close_time": "2026-07-05T05:00:00Z",
            "status": "settled",
            "volume_fp": "100.00",
        },
    ]
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/markets",
        category="markets_history",
        key="KXHIGHNY_settled",
        http_status=200,
        latency_ms=1,
        payload={"markets": markets},
    )

    def write_candles(*, ticker: str, spread: str, volume: str) -> None:
        bid = f"{0.50 - float(spread) / 2:.4f}"
        ask = f"{0.50 + float(spread) / 2:.4f}"
        writer.write(
            ts_utc=utc_now_iso(),
            endpoint=f"/historical/markets/{ticker}/candlesticks",
            category="candlesticks",
            key=ticker,
            http_status=200,
            latency_ms=1,
            payload={
                "ticker": ticker,
                "candlesticks": [
                    {
                        "end_period_ts": snapshot_ts,
                        "yes_bid": {"close": bid},
                        "yes_ask": {"close": ask},
                        "volume": volume,
                    }
                ],
            },
        )

    write_candles(ticker=ok_ticker, spread="0.10", volume="12.00")
    write_candles(ticker=bad_ticker, spread="0.50", volume="12.00")
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
    summary = pd.read_csv(out_dir / "spread_census.csv")

    for stem in ("median_spread", "n_spread_obs"):
        assert f"{stem}_exclnoreconcile" in summary.columns

    at_t24 = summary[(summary["horizon_h"] == 24) & (summary["band"] == "10_90")]
    carry = float(at_t24["median_spread_carryforward"].iloc[0])
    excl = float(at_t24["median_spread_exclnoreconcile"].iloc[0])
    assert carry != excl
    assert excl == pytest.approx(0.10)
    assert int(at_t24["n_spread_obs_carryforward"].iloc[0]) == 2
    assert int(at_t24["n_spread_obs_exclnoreconcile"].iloc[0]) == 1


def test_summarize_exclnoreconcile_drops_excluded_tickers() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "ticker": "KEEP",
                "climate_date": "2026-07-04",
                "horizon_h": 24,
                "era": "2022+",
                "season": "summer",
                "regime": None,
                "regime_uncertain": True,
                "in_trading_window": True,
                "two_sided_carryforward": True,
                "mid_carryforward": 0.5,
                "spread_carryforward": 0.05,
                "quote_present": True,
                "two_sided": True,
                "mid": 0.5,
                "spread": 0.05,
                "quote_valid": True,
                "extreme_empty_book": False,
                "volume": 1.0,
                "quote_age_sec": 60.0,
            },
            {
                "ticker": "DROP",
                "climate_date": "2026-07-04",
                "horizon_h": 24,
                "era": "2022+",
                "season": "summer",
                "regime": None,
                "regime_uncertain": True,
                "in_trading_window": True,
                "two_sided_carryforward": True,
                "mid_carryforward": 0.9,
                "spread_carryforward": 0.5,
                "quote_present": True,
                "two_sided": True,
                "mid": 0.9,
                "spread": 0.5,
                "quote_valid": True,
                "extreme_empty_book": False,
                "volume": 1.0,
                "quote_age_sec": 60.0,
            },
        ]
    )
    summary = summarize(snapshots, excl_noreconcile_tickers=frozenset({"DROP"}))
    row = summary[(summary["horizon_h"] == 24) & (summary["band"] == "10_90")].iloc[0]
    assert row["median_spread_carryforward"] == 0.275
    assert row["median_spread_exclnoreconcile"] == 0.05
    assert row["n_spread_obs_carryforward"] == 2
    assert row["n_spread_obs_exclnoreconcile"] == 1


@pytest.mark.parametrize(
    "climate_date",
    ["2025-06-02", "2025-06-03", "2025-06-18", "2025-11-13"],
)
def test_label_less_climate_days_degrade_gracefully(climate_date: str) -> None:
    # These four market climate days have no usable CLINYC label (data-sources O10).
    year = int(climate_date[:4])
    month = climate_date[5:7]
    day = climate_date[8:10]
    month_names = {
        "01": "JAN",
        "02": "FEB",
        "03": "MAR",
        "04": "APR",
        "05": "MAY",
        "06": "JUN",
        "07": "JUL",
        "08": "AUG",
        "09": "SEP",
        "10": "OCT",
        "11": "NOV",
        "12": "DEC",
    }
    yy = year % 100
    ticker = f"KXHIGHNY-{yy:02d}{month_names[month]}{int(day):02d}-T90"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    end_ts = int((t_end - timedelta(hours=24)).timestamp()) - 60
    candles = [
        candle_fields(
            {
                "end_period_ts": end_ts,
                "yes_bid": {"close_dollars": "0.40"},
                "yes_ask": {"close_dollars": "0.45"},
                "volume_fp": "1.00",
            }
        )
    ]
    snapshots, _stats = build_snapshot_table(
        markets=[
            {
                "ticker": ticker,
                "open_time": f"{climate_date}T14:00:00Z",
                "close_time": f"{climate_date}T23:59:00Z",
            }
        ],
        candles_by_ticker={ticker: candles},
        labels=pd.DataFrame(),
    )
    day_rows = snapshots[snapshots["climate_date"] == climate_date]
    assert not day_rows.empty
    assert day_rows["regime"].isna().all()
    assert bool(day_rows["regime_uncertain"].all())

    summary = summarize(snapshots)
    grouped = summary[(summary["horizon_h"] == 24) & (summary["band"] == "10_90")]
    assert int(grouped["n_market_days"].sum()) >= 1
    assert float(grouped["coverage"].max()) > 0.0
