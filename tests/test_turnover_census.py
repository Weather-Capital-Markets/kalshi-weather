"""Turnover census tests (Gate 0 input — premium/volume distributions)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from analysis.spread_census import era_of, parse_iso_utc
from analysis.turnover_census import (
    NOTE_ERA_SPLIT,
    NOTE_KALSHI_ONLY,
    NOTE_PREMIUM_ONCE,
    NOTE_UPPER_BOUND,
    aggregate_market_day,
    build_climate_day_table,
    build_market_day_table,
    candle_in_trading_window,
    candle_mid_price,
    run,
    summarize_turnover,
)
from ingestion.climate_day import climate_day_end
from ingestion.writer import RawJsonlWriter, utc_now_iso


def test_candle_in_trading_window_respects_open_and_close() -> None:
    open_dt = datetime(2024, 8, 14, 14, 0, tzinfo=timezone.utc)
    close_dt = datetime(2024, 8, 16, 3, 59, tzinfo=timezone.utc)
    before_open = int(open_dt.timestamp()) - 60
    after_close = int(close_dt.timestamp()) + 60
    inside = int((close_dt - timedelta(hours=3)).timestamp())
    assert not candle_in_trading_window(before_open, open_dt=open_dt, close_dt=close_dt)
    assert not candle_in_trading_window(after_close, open_dt=open_dt, close_dt=close_dt)
    assert candle_in_trading_window(inside, open_dt=open_dt, close_dt=close_dt)


def test_candle_mid_price_two_sided_and_trade_fallback() -> None:
    two_sided = {
        "end_period_ts": 1,
        "yes_bid": {"close_dollars": "0.40"},
        "yes_ask": {"close_dollars": "0.50"},
        "volume_fp": "0.00",
    }
    fields = {"bid_close": 0.40, "ask_close": 0.50, "volume": 0.0}
    assert candle_mid_price(two_sided, fields) == pytest.approx(0.45)

    trade_only = {
        "end_period_ts": 1,
        "price": {"close_dollars": "0.62"},
        "yes_bid": {"close_dollars": "0.0000"},
        "yes_ask": {"close_dollars": "1.0000"},
        "volume_fp": "5.00",
    }
    trade_fields = {"bid_close": 0.0, "ask_close": 1.0, "volume": 5.0}
    assert candle_mid_price(trade_only, trade_fields) == pytest.approx(0.62)


def test_aggregate_market_day_premium_band_and_vol_concentration() -> None:
    open_dt = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    close_dt = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)
    close_ts = int(close_dt.timestamp())
    candles = [
        {
            "end_period_ts": close_ts - 30 * 60,
            "yes_bid": {"close_dollars": "0.40"},
            "yes_ask": {"close_dollars": "0.50"},
            "volume_fp": "10.00",
        },
        {
            "end_period_ts": close_ts - 2 * 3600,
            "yes_bid": {"close_dollars": "0.02"},
            "yes_ask": {"close_dollars": "0.04"},
            "volume_fp": "4.00",
        },
        {
            "end_period_ts": close_ts - 5 * 3600,
            "yes_bid": {"close_dollars": "0.40"},
            "yes_ask": {"close_dollars": "0.50"},
            "volume_fp": "6.00",
        },
    ]
    all_band = aggregate_market_day(
        candles,
        open_dt=open_dt,
        close_dt=close_dt,
        band_lo=None,
        band_hi=None,
    )
    assert all_band["contracts_traded"] == pytest.approx(20.0)
    assert all_band["premium_traded"] == pytest.approx(10 * 0.45 + 4 * 0.03 + 6 * 0.45)
    assert all_band["share_vol_last_1h"] == pytest.approx(10 / 20)
    assert all_band["share_vol_last_3h"] == pytest.approx(14 / 20)
    assert all_band["share_vol_last_6h"] == pytest.approx(20 / 20)

    mid_band = aggregate_market_day(
        candles,
        open_dt=open_dt,
        close_dt=close_dt,
        band_lo=0.10,
        band_hi=0.90,
    )
    assert mid_band["contracts_traded"] == pytest.approx(16.0)
    assert mid_band["premium_traded"] == pytest.approx(10 * 0.45 + 6 * 0.45)


def test_build_climate_day_table_sums_brackets() -> None:
    market_days = pd.DataFrame(
        [
            {
                "ticker": "KXHIGHNY-26JUL04-T90",
                "climate_date": "2026-07-04",
                "season": "JJA",
                "era": "2022_plus",
                "band": "10_90",
                "premium_traded": 10.0,
                "contracts_traded": 20.0,
                "share_vol_last_1h": 0.5,
                "share_vol_last_3h": 0.7,
                "share_vol_last_6h": 1.0,
            },
            {
                "ticker": "KXHIGHNY-26JUL04-T91",
                "climate_date": "2026-07-04",
                "season": "JJA",
                "era": "2022_plus",
                "band": "10_90",
                "premium_traded": 5.0,
                "contracts_traded": 8.0,
                "share_vol_last_1h": 0.25,
                "share_vol_last_3h": 0.5,
                "share_vol_last_6h": 0.8,
            },
        ]
    )
    climate = build_climate_day_table(market_days)
    row = climate.iloc[0]
    assert row["premium_traded_climate_day"] == pytest.approx(15.0)
    assert row["contracts_traded_climate_day"] == pytest.approx(28.0)
    assert row["n_brackets"] == 2
    assert row["month"] == "2026-07"


def test_era_split_keeps_2021_separate() -> None:
    assert era_of("2021-08-05") == "2021_early"
    assert era_of("2022-01-01") == "2022_plus"


def test_summarize_turnover_includes_distribution_columns() -> None:
    market_days = pd.DataFrame(
        [
            {
                "ticker": "A",
                "climate_date": "2026-07-04",
                "season": "JJA",
                "era": "2022_plus",
                "band": "10_90",
                "premium_traded": 10.0,
                "contracts_traded": 20.0,
                "share_vol_last_1h": 0.5,
                "share_vol_last_3h": 0.7,
                "share_vol_last_6h": 1.0,
            }
        ]
    )
    climate_days = build_climate_day_table(market_days)
    summary = summarize_turnover(market_days, climate_days)
    row = summary[(summary["band"] == "10_90") & (summary["season"] == "JJA")].iloc[0]
    assert row["n_market_days"] == 1
    assert row["median_premium_climate_day"] == pytest.approx(10.0)
    assert "climate_day_premium_p10" in summary.columns


def test_turnover_writes_csv_and_pngs(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    climate_date = "2026-07-04"
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    close_dt = parse_iso_utc("2026-07-05T05:00:00Z")
    assert close_dt is not None
    end_ts = int(close_dt.timestamp()) - 2 * 3600
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
                    "yes_ask": {"close_dollars": "0.50"},
                    "volume_fp": "12.00",
                },
                {
                    "end_period_ts": int((t_end - timedelta(hours=1)).timestamp()),
                    "yes_bid": {"close_dollars": "0.40"},
                    "yes_ask": {"close_dollars": "0.50"},
                    "volume_fp": "3.00",
                },
            ],
        },
    )
    writer.close()

    out_dir = tmp_path / "out"
    config = {"storage": {"raw_dir": str(raw_dir)}}
    assert run(config, out_dir) == 0

    csv_path = out_dir / "turnover_census.csv"
    assert csv_path.exists()
    header = csv_path.read_text(encoding="utf-8").splitlines()[:4]
    assert NOTE_UPPER_BOUND in header[0]
    assert NOTE_PREMIUM_ONCE in header[1]
    assert NOTE_ERA_SPLIT in header[2]
    assert NOTE_KALSHI_ONLY in header[3]

    summary = pd.read_csv(csv_path, comment="#")
    for column in (
        "era",
        "season",
        "band",
        "median_premium_climate_day",
        "climate_day_premium_median",
        "mean_share_vol_last_1h",
    ):
        assert column in summary.columns

    assert (out_dir / "turnover_census_monthly.csv").exists()
    for name in (
        "turnover_census_premium_dist.png",
        "turnover_census_monthly_trend.png",
        "turnover_census_vol_concentration.png",
    ):
        assert (out_dir / name).exists()


def test_build_market_day_table_emits_both_bands(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    ticker = "KXHIGHNY-26JUL04-T90"
    close_dt = parse_iso_utc("2026-07-05T05:00:00Z")
    assert close_dt is not None
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
                    "end_period_ts": int(close_dt.timestamp()) - 3600,
                    "yes_bid": {"close_dollars": "0.40"},
                    "yes_ask": {"close_dollars": "0.50"},
                    "volume_fp": "10.00",
                }
            ],
        },
    )
    writer.close()

    from analysis.spread_census import load_markets
    from analysis.turnover_census import load_raw_candles_by_ticker

    markets = load_markets(raw_dir)
    candles = load_raw_candles_by_ticker(raw_dir)
    table = build_market_day_table(markets, candles)
    bands = set(table["band"])
    assert bands == {"10_90", "all"}
    assert len(table) == 2
