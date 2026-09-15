"""Turnover M and A from the trade parquet, not candles.

M = mean daily premium in the 10–90¢ band, plus p90, p99, top-decile share,
and annual pot 365 × mean. A = average trade price in band.

Do not substitute median for mean. Do not tune to the candle census.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START

MID_BAND_LO = 0.10
MID_BAND_HI = 0.90


def load_trade_frame(parquet_dir: Path) -> pl.DataFrame:
    files = sorted(p for p in parquet_dir.glob("*.parquet") if p.is_file())
    if not files:
        nested = parquet_dir / "_tickers"
        files = sorted(nested.glob("*.parquet")) if nested.is_dir() else []
    if not files:
        raise FileNotFoundError(f"no parquet in {parquet_dir}")
    frame = pl.concat([pl.read_parquet(path) for path in files], how="vertical")
    return frame.unique(subset=["trade_id"], keep="last")


def with_premium(frame: pl.DataFrame) -> pl.DataFrame:
    yes = pl.col("yes_price").cast(pl.Float64)
    count = pl.col("count").cast(pl.Float64)
    return frame.with_columns(
        yes.alias("price"),
        (yes * count).alias("premium"),
        count.alias("contracts"),
    )


def in_band(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(
        (pl.col("price") >= MID_BAND_LO) & (pl.col("price") <= MID_BAND_HI)
    )


def m_and_a(frame: pl.DataFrame) -> dict[str, Any]:
    priced = with_premium(frame)
    band = in_band(priced)
    if band.is_empty():
        return {
            "n_trades_all": priced.height,
            "n_trades_band": 0,
            "M": None,
            "A": None,
            "note": "no trades in the 10-90c band",
        }
    daily = (
        band.group_by("climate_day")
        .agg(
            pl.col("premium").sum().alias("daily_premium"),
            pl.col("contracts").sum().alias("daily_contracts"),
            pl.col("premium").count().alias("n_trades"),
        )
        .sort("climate_day")
    )
    prem = daily.get_column("daily_premium")
    mean = float(prem.mean())
    p90 = float(prem.quantile(0.90))
    p99 = float(prem.quantile(0.99))
    ordered = prem.sort(descending=True)
    top_n = max(int(round(ordered.len() * 0.10)), 1)
    top_share = float(ordered.head(top_n).sum() / ordered.sum()) if float(ordered.sum()) else None
    avg_price = float(
        (band.get_column("premium").sum()) / (band.get_column("contracts").sum())
    )
    return {
        "n_trades_all": priced.height,
        "n_trades_band": band.height,
        "n_climate_days": daily.height,
        "band": [MID_BAND_LO, MID_BAND_HI],
        "M": {
            "mean_daily_premium": mean,
            "p90_daily_premium": p90,
            "p99_daily_premium": p99,
            "top_decile_share": top_share,
            "annual_pot_365x_mean": 365.0 * mean,
        },
        "A": {"average_trade_price_in_band": avg_price},
        "used_median_for_mean": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--start", type=date.fromisoformat, default=SIX_BRACKET_ERA_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, default=Path("analysis/out/trade_turnover.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    frame = load_trade_frame(args.parquet)
    climate = pl.col("climate_day").cast(pl.Utf8)
    frame = frame.filter(climate >= args.start.isoformat())
    if args.end is not None:
        frame = frame.filter(climate <= args.end.isoformat())
    payload = m_and_a(frame)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {args.out}")
    return 0 if payload.get("M") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
