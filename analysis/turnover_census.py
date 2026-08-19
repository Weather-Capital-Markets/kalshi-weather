"""Turnover census over historical Kalshi KXHIGHNY candlesticks (Gate 0 input).

Allowed DB: none. Reads frozen raw JSONL only; never opens heartbeat.sqlite
or backfill.sqlite.

Measures premium and contract volume actually traded — an upper bound on what
a maker could capture. Prints distributions only; no Gate 0 arithmetic.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import (
    _float,
    candle_fields,
    era_of,
    is_two_sided,
    load_candles,
    load_markets,
    parse_iso_utc,
    season_of,
    ticker_climate_date,
)
from ingestion.config_loader import load_config

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

NOTE_UPPER_BOUND = (
    "NOTE: volume traded is an UPPER BOUND on what a maker could capture — it "
    "includes trades between other participants, and a maker captures only the "
    "fraction they are on the other side of. Fill share is unmeasured and "
    "requires the logger (K4)."
)
NOTE_PREMIUM_ONCE = (
    "NOTE: premium traded counts each trade once from the taker side; do not "
    "double it."
)
NOTE_ERA_SPLIT = (
    "NOTE: 2021 is a distinct density regime (~1.3 brackets/day vs ~6/day from "
    "2022 on); do not pool 2021 with the modern era at readout."
)
NOTE_KALSHI_ONLY = (
    "NOTE: Kalshi only. Polymarket has no historical equivalent — its volume "
    "history is not captured and cannot be backfilled."
)

BAND_SPECS: tuple[tuple[str, float | None, float | None], ...] = (
    ("10_90", 0.10, 0.90),
    ("all", None, None),
)


def candle_mid_price(raw: dict[str, Any], fields: dict[str, Any]) -> float | None:
    bid = fields.get("bid_close")
    ask = fields.get("ask_close")
    if is_two_sided(bid, ask) and bid is not None and ask is not None:
        return (bid + ask) / 2.0
    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    return _float(price.get("close_dollars", price.get("close")))


def candle_in_trading_window(
    end_period_ts: int,
    *,
    open_dt: datetime | None,
    close_dt: datetime | None,
) -> bool:
    candle_dt = datetime.fromtimestamp(end_period_ts, tz=timezone.utc)
    if open_dt is not None and candle_dt < open_dt:
        return False
    if close_dt is not None and candle_dt > close_dt:
        return False
    return True


def load_raw_candles_by_ticker(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load raw candle dicts per ticker (for price.close fallback)."""
    from analysis.spread_census import _ticker_from_candle_record, iter_category

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for record in iter_category(raw_dir, "candlesticks"):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        ticker = _ticker_from_candle_record(record, payload)
        if not ticker:
            continue
        candles = payload.get("candlesticks")
        if not isinstance(candles, list):
            continue
        bucket = by_ticker.setdefault(ticker, [])
        for candle in candles:
            if isinstance(candle, dict):
                bucket.append(candle)
    for ticker in by_ticker:
        by_ticker[ticker].sort(
            key=lambda c: int(c["end_period_ts"])
            if isinstance(c.get("end_period_ts"), (int, float))
            else 0
        )
    return by_ticker


def aggregate_market_day(
    raw_candles: list[dict[str, Any]],
    *,
    open_dt: datetime | None,
    close_dt: datetime | None,
    band_lo: float | None,
    band_hi: float | None,
) -> dict[str, Any]:
    contracts = 0.0
    premium = 0.0
    active_minutes = 0
    vol_last_1h = 0.0
    vol_last_3h = 0.0
    vol_last_6h = 0.0
    close_ts = int(close_dt.timestamp()) if close_dt is not None else None

    for raw in raw_candles:
        end_ts = raw.get("end_period_ts")
        if not isinstance(end_ts, (int, float)):
            continue
        ts = int(end_ts)
        if not candle_in_trading_window(ts, open_dt=open_dt, close_dt=close_dt):
            continue
        fields = candle_fields(raw)
        volume = fields.get("volume")
        if volume is None or volume <= 0:
            continue
        mid = candle_mid_price(raw, fields)
        if band_lo is not None:
            if mid is None or mid < band_lo or mid > band_hi:
                continue
        active_minutes += 1
        contracts += volume
        if mid is not None:
            premium += volume * mid
        if close_ts is not None:
            hours_to_close = (close_ts - ts) / 3600.0
            if hours_to_close <= 1:
                vol_last_1h += volume
            if hours_to_close <= 3:
                vol_last_3h += volume
            if hours_to_close <= 6:
                vol_last_6h += volume

    total = contracts
    return {
        "contracts_traded": contracts,
        "premium_traded": premium,
        "active_minutes": active_minutes,
        "share_vol_last_1h": (vol_last_1h / total) if total else float("nan"),
        "share_vol_last_3h": (vol_last_3h / total) if total else float("nan"),
        "share_vol_last_6h": (vol_last_6h / total) if total else float("nan"),
    }


def build_market_day_table(
    markets: list[dict[str, Any]],
    raw_candles_by_ticker: dict[str, list[dict[str, Any]]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate_date = ticker_climate_date(ticker)
        if not climate_date:
            continue
        open_dt = parse_iso_utc(
            market.get("open_time") if isinstance(market.get("open_time"), str) else None
        )
        close_dt = parse_iso_utc(
            market.get("close_time") if isinstance(market.get("close_time"), str) else None
        )
        raw_candles = raw_candles_by_ticker.get(ticker, [])
        base = {
            "ticker": ticker,
            "climate_date": climate_date,
            "season": season_of(climate_date),
            "era": era_of(climate_date),
        }
        for band_name, band_lo, band_hi in BAND_SPECS:
            metrics = aggregate_market_day(
                raw_candles,
                open_dt=open_dt,
                close_dt=close_dt,
                band_lo=band_lo,
                band_hi=band_hi,
            )
            rows.append({**base, "band": band_name, **metrics})
    return pd.DataFrame(rows)


def build_climate_day_table(market_days: pd.DataFrame) -> pd.DataFrame:
    if market_days.empty:
        return market_days
    grouped = market_days.groupby(
        ["climate_date", "season", "era", "band"],
        as_index=False,
    ).agg(
        premium_traded_climate_day=("premium_traded", "sum"),
        contracts_traded_climate_day=("contracts_traded", "sum"),
        n_brackets=("ticker", "nunique"),
        share_vol_last_1h=("share_vol_last_1h", "mean"),
        share_vol_last_3h=("share_vol_last_3h", "mean"),
        share_vol_last_6h=("share_vol_last_6h", "mean"),
    )
    grouped["month"] = grouped["climate_date"].str.slice(0, 7)
    return grouped


def _distribution_stats(series: pd.Series) -> dict[str, float]:
    clean = series.dropna()
    if clean.empty:
        return {
            "p10": float("nan"),
            "p25": float("nan"),
            "median": float("nan"),
            "p75": float("nan"),
            "p90": float("nan"),
        }
    return {
        "p10": float(clean.quantile(0.10)),
        "p25": float(clean.quantile(0.25)),
        "median": float(clean.median()),
        "p75": float(clean.quantile(0.75)),
        "p90": float(clean.quantile(0.90)),
    }


def summarize_turnover(
    market_days: pd.DataFrame,
    climate_days: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for band in market_days["band"].unique():
        md = market_days[market_days["band"] == band]
        cd = climate_days[climate_days["band"] == band]
        for era in sorted(md["era"].unique()):
            for season in ("DJF", "MAM", "JJA", "SON"):
                md_slice = md[(md["era"] == era) & (md["season"] == season)]
                cd_slice = cd[(cd["era"] == era) & (cd["season"] == season)]
                if md_slice.empty and cd_slice.empty:
                    continue
                dist = _distribution_stats(cd_slice["premium_traded_climate_day"])
                rows.append(
                    {
                        "era": era,
                        "season": season,
                        "band": band,
                        "n_market_days": int(len(md_slice)),
                        "n_climate_days": int(cd_slice["climate_date"].nunique()),
                        "mean_premium_market_day": (
                            float(md_slice["premium_traded"].mean())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "median_premium_market_day": (
                            float(md_slice["premium_traded"].median())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "mean_premium_climate_day": (
                            float(cd_slice["premium_traded_climate_day"].mean())
                            if len(cd_slice)
                            else float("nan")
                        ),
                        "median_premium_climate_day": (
                            float(cd_slice["premium_traded_climate_day"].median())
                            if len(cd_slice)
                            else float("nan")
                        ),
                        "median_contracts_market_day": (
                            float(md_slice["contracts_traded"].median())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "mean_share_vol_last_1h": (
                            float(md_slice["share_vol_last_1h"].mean())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "mean_share_vol_last_3h": (
                            float(md_slice["share_vol_last_3h"].mean())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "mean_share_vol_last_6h": (
                            float(md_slice["share_vol_last_6h"].mean())
                            if len(md_slice)
                            else float("nan")
                        ),
                        **{f"climate_day_premium_{k}": v for k, v in dist.items()},
                    }
                )
    return pd.DataFrame(rows)


def build_monthly_trend(climate_days: pd.DataFrame) -> pd.DataFrame:
    if climate_days.empty:
        return climate_days
    return (
        climate_days.groupby(["month", "era", "band"], as_index=False)["premium_traded_climate_day"]
        .median()
        .rename(columns={"premium_traded_climate_day": "median_premium_climate_day"})
        .sort_values(["band", "era", "month"])
    )


def write_outputs(
    summary: pd.DataFrame,
    monthly: pd.DataFrame,
    climate_days: pd.DataFrame,
    out_dir: Path,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "turnover_census.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTE_UPPER_BOUND}\n")
        handle.write(f"# {NOTE_PREMIUM_ONCE}\n")
        handle.write(f"# {NOTE_ERA_SPLIT}\n")
        handle.write(f"# {NOTE_KALSHI_ONLY}\n")
    summary.to_csv(csv_path, mode="a", index=False)
    monthly_path = out_dir / "turnover_census_monthly.csv"
    monthly.to_csv(monthly_path, index=False)
    return csv_path, monthly_path


def _plot_premium_distribution(climate_days: pd.DataFrame, path: Path) -> None:
    plot = climate_days[
        (climate_days["band"] == "10_90") & (climate_days["era"] == "2022_plus")
    ]
    if plot.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    for season, group in plot.groupby("season"):
        if group.empty:
            continue
        ax.hist(
            group["premium_traded_climate_day"],
            bins=30,
            alpha=0.5,
            label=season,
        )
    ax.set_xlabel("premium traded per climate day ($, 10-90c band)")
    ax.set_ylabel("climate days")
    ax.set_title("Per-climate-day premium distribution (2022+, by season)")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _plot_monthly_trend(monthly: pd.DataFrame, path: Path) -> None:
    plot = monthly[(monthly["band"] == "10_90") & (monthly["era"] == "2022_plus")]
    if plot.empty:
        return
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(plot["month"], plot["median_premium_climate_day"], marker=".")
    ax.set_xlabel("month")
    ax.set_ylabel("median premium per climate day ($)")
    ax.set_title("Monthly trend (10-90c band, 2022+)")
    plt.xticks(rotation=45, ha="right")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _plot_vol_concentration(summary: pd.DataFrame, path: Path) -> None:
    plot = summary[(summary["band"] == "10_90") & (summary["era"] == "2022_plus")]
    if plot.empty:
        return
    seasons = ["DJF", "MAM", "JJA", "SON"]
    x = range(len(seasons))
    width = 0.25
    fig, ax = plt.subplots(figsize=(8, 4))
    for idx, col in enumerate(
        ("mean_share_vol_last_1h", "mean_share_vol_last_3h", "mean_share_vol_last_6h")
    ):
        values = [
            float(plot.loc[plot["season"] == s, col].iloc[0])
            if not plot.loc[plot["season"] == s].empty
            else 0.0
            for s in seasons
        ]
        offset = (idx - 1) * width
        label = col.replace("mean_share_vol_last_", "≤")
        ax.bar([i + offset for i in x], values, width=width, label=label)
    ax.set_xticks(list(x))
    ax.set_xticklabels(seasons)
    ax.set_ylabel("share of contract volume")
    ax.set_title("Volume concentration before close (10-90c, 2022+)")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _print_headline(summary: pd.DataFrame) -> None:
    primary = summary[summary["band"] == "10_90"].sort_values(["era", "season"])
    if primary.empty:
        print("no turnover rows in 10-90c band")
        return
    print("\n=== headline: median premium per climate day ($, 10-90c band) ===")
    for row in primary.itertuples(index=False):
        print(
            f"era={row.era} season={row.season} "
            f"median_climate_day={row.median_premium_climate_day:.2f} "
            f"mean_climate_day={row.mean_premium_climate_day:.2f} "
            f"p10={row.climate_day_premium_p10:.2f} "
            f"p90={row.climate_day_premium_p90:.2f} "
            f"n_climate_days={row.n_climate_days}"
        )


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    markets = load_markets(raw_dir)
    raw_candles = load_raw_candles_by_ticker(raw_dir)
    # Touch load_candles to ensure same ticker coverage path is valid in tests
    _ = load_candles(raw_dir)

    print("Turnover census: measurement only (no Gate 0 arithmetic)")
    print(NOTE_UPPER_BOUND)
    print(NOTE_PREMIUM_ONCE)
    print(NOTE_ERA_SPLIT)
    print(NOTE_KALSHI_ONLY)

    if not markets:
        print("no markets_history data in raw_dir")
        return 0

    market_days = build_market_day_table(markets, raw_candles)
    climate_days = build_climate_day_table(market_days)
    summary = summarize_turnover(market_days, climate_days)
    monthly = build_monthly_trend(climate_days)

    csv_path, monthly_path = write_outputs(summary, monthly, climate_days, out_dir)
    dist_png = out_dir / "turnover_census_premium_dist.png"
    trend_png = out_dir / "turnover_census_monthly_trend.png"
    conc_png = out_dir / "turnover_census_vol_concentration.png"
    _plot_premium_distribution(climate_days, dist_png)
    _plot_monthly_trend(monthly, trend_png)
    _plot_vol_concentration(summary, conc_png)

    print(f"\nsummary csv: {csv_path}")
    print(f"monthly csv: {monthly_path}")
    print(f"premium distribution: {dist_png}")
    print(f"monthly trend: {trend_png}")
    print(f"volume concentration: {conc_png}")
    _print_headline(summary)

    all_band = summary[summary["band"] == "all"].sort_values(["era", "season"])
    if not all_band.empty:
        print("\n=== full price range (band=all), median premium per climate day ===")
        for row in all_band.itertuples(index=False):
            print(
                f"era={row.era} season={row.season} "
                f"median_climate_day={row.median_premium_climate_day:.2f} "
                f"n_climate_days={row.n_climate_days}"
            )

    if not summary.empty:
        print("\n=== full summary table ===")
        print(summary.to_string(index=False))

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Historical turnover census (Gate 0 input)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    turnover_cfg = config.get("turnover_census") or {}
    out_dir = args.out_dir or Path(str(turnover_cfg.get("out_dir") or "analysis/out"))
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
