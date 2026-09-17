"""Phase B6 turnover reporter from TRADE parquet (not candles) for v0 RUN."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from analysis.spread_census import ticker_climate_date
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START

MID_BAND_LO = 0.10
MID_BAND_HI = 0.90
ERA_START = SIX_BRACKET_ERA_START

RECORDED = {
    "median_daily_premium_climate_day": 2400.0,
    "p75_daily_premium_climate_day_range": (15000.0, 26000.0),
    "median_brackets_with_volume_per_climate_day": 4.0,
    "median_premium_per_market_day": 214.0,
    "zero_volume_market_day_share_pct": 22.3,
}

NOTE_GATE0 = (
    "M and A are threshold inputs for Gate 0 / C1-X1, not outcomes of a test."
)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    series = pl.Series("v", values).sort()
    return float(series.quantile(q, interpolation="linear"))


def _load_markets(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list of markets")
    return [row for row in payload if isinstance(row, dict)]


def _market_climate_days(markets: list[dict[str, Any]], *, start: date) -> pl.DataFrame:
    rows: list[dict[str, str]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate_date = ticker_climate_date(ticker)
        if climate_date is None:
            continue
        if date.fromisoformat(climate_date) < start:
            continue
        rows.append({"ticker": ticker, "climate_day": climate_date})
    if not rows:
        return pl.DataFrame(schema={"ticker": pl.Utf8, "climate_day": pl.Utf8})
    return pl.DataFrame(rows)


def turnover_from_trades(
    trades_parquet: Path | str,
    markets_path: Path | str,
    *,
    start: date = ERA_START,
    end: date | None = None,
) -> dict[str, Any]:
    """Turnover census on trade tape in the 10–90¢ YES-price band."""
    parquet_root = Path(trades_parquet)
    if parquet_root.is_dir():
        files = sorted(parquet_root.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"no parquet shards under {parquet_root}")
        frame = pl.scan_parquet([str(path) for path in files])
    else:
        frame = pl.scan_parquet(str(parquet_root))

    markets = _load_markets(Path(markets_path))
    market_days = _market_climate_days(markets, start=start)

    trades = (
        frame.select(
            pl.col("ticker").cast(pl.Utf8),
            pl.col("climate_day").cast(pl.Utf8),
            pl.col("yes_price").cast(pl.Float64),
            pl.col("count").cast(pl.Float64),
        )
        .filter(pl.col("climate_day").is_not_null())
        .filter(pl.col("yes_price").is_between(MID_BAND_LO, MID_BAND_HI))
        .collect()
    )

    if end is not None:
        end_s = end.isoformat()
        trades = trades.filter(
            (pl.col("climate_day") >= start.isoformat()) & (pl.col("climate_day") <= end_s)
        )
    else:
        trades = trades.filter(pl.col("climate_day") >= start.isoformat())

    if trades.is_empty():
        return {
            "status": "empty",
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "note": NOTE_GATE0,
        }

    trades = trades.with_columns(
        (pl.col("yes_price") * pl.col("count")).alias("premium"),
    )

    climate_daily = trades.group_by("climate_day").agg(
        pl.col("premium").sum().alias("premium"),
        pl.col("count").sum().alias("contracts"),
        pl.col("ticker").n_unique().alias("n_brackets_with_volume"),
    ).sort("climate_day")

    market_daily = trades.group_by(["climate_day", "ticker"]).agg(
        pl.col("premium").sum().alias("premium"),
        pl.col("count").sum().alias("contracts"),
        pl.col("yes_price").mean().alias("trade_weighted_avg_price"),
    )

    daily_premiums = [float(v) for v in climate_daily["premium"].to_list()]
    total_premium = float(sum(daily_premiums))
    sorted_premiums = sorted(daily_premiums, reverse=True)
    top_n = max(1, int(len(sorted_premiums) * 0.10 + 0.999999))
    top_decile_share = (
        float(sum(sorted_premiums[:top_n]) / total_premium) if total_premium else None
    )

    total_contracts = float(trades["count"].sum())
    volume_weighted_avg_price = (
        float(trades["premium"].sum() / total_contracts) if total_contracts else None
    )
    trade_weighted_avg_price = float(trades["yes_price"].mean())

    market_premiums = [float(v) for v in market_daily["premium"].to_list()]
    brackets_with_volume = [
        float(v) for v in climate_daily["n_brackets_with_volume"].to_list()
    ]

    if market_days.is_empty():
        zero_volume_share = None
    else:
        traded_keys = market_daily.select(["climate_day", "ticker"]).unique()
        all_keys = market_days.select(["climate_day", "ticker"]).unique()
        joined = all_keys.join(traded_keys, on=["climate_day", "ticker"], how="anti")
        zero_volume_share = float(joined.height / all_keys.height) if all_keys.height else None

    measured = {
        "M_mean_daily_premium": float(sum(daily_premiums) / len(daily_premiums)),
        "median_daily_premium": _quantile(daily_premiums, 0.50),
        "p75_daily_premium": _quantile(daily_premiums, 0.75),
        "p90_daily_premium": _quantile(daily_premiums, 0.90),
        "p99_daily_premium": _quantile(daily_premiums, 0.99),
        "top_decile_share": top_decile_share,
        "A_volume_weighted_avg_price": volume_weighted_avg_price,
        "A_trade_weighted_avg_price": trade_weighted_avg_price,
        "mean_brackets_with_volume_per_climate_day": (
            float(sum(brackets_with_volume) / len(brackets_with_volume))
            if brackets_with_volume
            else None
        ),
        "median_brackets_with_volume_per_climate_day": _quantile(brackets_with_volume, 0.50),
        "median_premium_per_market_day": _quantile(market_premiums, 0.50),
        "zero_volume_market_day_share": zero_volume_share,
        "zero_volume_market_day_share_pct": (
            zero_volume_share * 100.0 if zero_volume_share is not None else None
        ),
        "n_climate_days": len(daily_premiums),
        "n_market_days_with_band_volume": len(market_premiums),
        "n_trades_in_band": int(trades.height),
        "total_premium_in_band": total_premium,
        "total_contracts_in_band": total_contracts,
    }

    return {
        "status": "OK",
        "band": "10_90",
        "premium_definition": (
            "yes_price * count per trade (YES-space; tape analogue of "
            "turnover_census mid*volume)"
        ),
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "measured": measured,
        "recorded": RECORDED,
        "note": NOTE_GATE0,
    }
