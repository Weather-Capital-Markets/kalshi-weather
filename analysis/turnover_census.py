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
from typing import Any, Iterator

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import (
    _float,
    candle_fields,
    era_of,
    is_two_sided,
    load_markets,
    parse_iso_utc,
    season_of,
    ticker_climate_date,
)
from ingestion.config_loader import load_config
from ingestion.validate_units import assert_non_empty_frame
from ingestion.writer import read_jsonl_gz

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

NOTE_UPPER_BOUND = (
    "NOTE: volume traded is an UPPER BOUND on what a maker could capture — it "
    "includes trades between other participants, and a maker captures only the "
    "fraction they are on the other side of. Fill share is unmeasured and "
    "requires the logger (K4)."
)
NOTE_PREMIUM_ONCE = (
    "NOTE: premium traded counts each trade once from the taker side; do not " "double it."
)
NOTE_ERA_SPLIT = (
    "NOTE: 2021 is a distinct density regime (~1.3 brackets/day vs ~6/day from "
    "2022 on); do not pool 2021 with the modern era at readout."
)
NOTE_KALSHI_ONLY = (
    "NOTE: Kalshi only. Polymarket has no historical equivalent — its volume "
    "history is not captured and cannot be backfilled."
)
NOTE_MEDIAN_OF_RATIOS = (
    "NOTE: median_share_vol_last_Nh is the median of per-market-day ratios "
    "(zero-volume days dropped). pooled_share_vol_last_Nh = Σ vol_last_Nh / "
    "Σ contracts. Use the pooled share for where volume goes; the median "
    "describes the typical dead bracket-day."
)

BAND_NAMES: tuple[str, ...] = ("10_90", "tails", "all")
MID_BAND_LO = 0.10
MID_BAND_HI = 0.90
TIME_TO_CLOSE_EDGES_H = (1, 3, 6, 12, 24, 48)
SEASONS = ("DJF", "MAM", "JJA", "SON")


def candle_mid_price(raw: dict[str, Any], fields: dict[str, Any]) -> float | None:
    bid = fields.get("bid_close")
    ask = fields.get("ask_close")
    if is_two_sided(bid, ask) and bid is not None and ask is not None:
        return (bid + ask) / 2.0
    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    return _float(price.get("close_dollars", price.get("close")))


def price_region(mid: float | None) -> str | None:
    """Assign a candle mid to 10-90c or tails; None when mid is unknown."""
    if mid is None:
        return None
    if MID_BAND_LO <= mid <= MID_BAND_HI:
        return "10_90"
    return "tails"


def candle_matches_band(mid: float | None, band: str) -> bool:
    region = price_region(mid)
    if band == "all":
        return True
    if band == "10_90":
        return region == "10_90"
    if band == "tails":
        return region == "tails"
    raise ValueError(f"unknown band {band!r}")


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


def time_to_close_bucket(hours_to_close: float) -> str:
    """Bucket hours-to-close for the volume-vs-time plot."""
    if hours_to_close <= 1:
        return "0-1h"
    if hours_to_close <= 3:
        return "1-3h"
    if hours_to_close <= 6:
        return "3-6h"
    if hours_to_close <= 12:
        return "6-12h"
    if hours_to_close <= 24:
        return "12-24h"
    if hours_to_close <= 48:
        return "24-48h"
    return "48h+"


def load_candles_for_ticker(raw_dir: Path, ticker: str) -> list[dict[str, Any]]:
    """Load raw candle dicts for one ticker without holding the full corpus."""
    candles: list[dict[str, Any]] = []
    safe = ticker.replace("/", "_")
    for path in raw_dir.glob(f"*/candlesticks/{safe}.jsonl.gz"):
        for record in read_jsonl_gz(path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            chunk = payload.get("candlesticks")
            if not isinstance(chunk, list):
                continue
            for candle in chunk:
                if isinstance(candle, dict):
                    candles.append(candle)
    candles.sort(
        key=lambda c: int(c["end_period_ts"])
        if isinstance(c.get("end_period_ts"), (int, float))
        else 0
    )
    return candles


def iter_market_candles(
    raw_dir: Path,
    markets: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Yield (market, candles) one market at a time."""
    for market in markets:
        ticker = str(market.get("ticker") or "")
        if not ticker:
            continue
        yield market, load_candles_for_ticker(raw_dir, ticker)


def aggregate_market_day(
    raw_candles: list[dict[str, Any]],
    *,
    open_dt: datetime | None,
    close_dt: datetime | None,
    band: str,
) -> dict[str, Any]:
    contracts = 0.0
    premium = 0.0
    active_minutes = 0
    vol_last_1h = 0.0
    vol_last_3h = 0.0
    vol_last_6h = 0.0
    time_bucket_volumes: dict[str, float] = {}
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
        if not candle_matches_band(mid, band):
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
            bucket = time_to_close_bucket(hours_to_close)
            time_bucket_volumes[bucket] = time_bucket_volumes.get(bucket, 0.0) + volume

    total = contracts
    return {
        "contracts_traded": contracts,
        "premium_traded": premium,
        "active_minutes": active_minutes,
        "vol_last_1h": vol_last_1h,
        "vol_last_3h": vol_last_3h,
        "vol_last_6h": vol_last_6h,
        "share_vol_last_1h": (vol_last_1h / total) if total else float("nan"),
        "share_vol_last_3h": (vol_last_3h / total) if total else float("nan"),
        "share_vol_last_6h": (vol_last_6h / total) if total else float("nan"),
        "time_bucket_volumes": time_bucket_volumes,
    }


def build_market_day_table(
    raw_dir: Path,
    markets: list[dict[str, Any]],
) -> tuple[pd.DataFrame, pd.Series]:
    rows: list[dict[str, Any]] = []
    bucket_rows: list[dict[str, Any]] = []
    for market, raw_candles in iter_market_candles(raw_dir, markets):
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
        base = {
            "ticker": ticker,
            "climate_date": climate_date,
            "season": season_of(climate_date),
            "era": era_of(climate_date),
        }
        for band_name in BAND_NAMES:
            metrics = aggregate_market_day(
                raw_candles,
                open_dt=open_dt,
                close_dt=close_dt,
                band=band_name,
            )
            time_buckets = metrics.pop("time_bucket_volumes")
            row = {**base, "band": band_name, **metrics}
            rows.append(row)
            bucket_rows.append({"row_idx": len(rows) - 1, "time_bucket_volumes": time_buckets})
    if not rows:
        return pd.DataFrame(), pd.Series(dtype=object)
    frame = pd.DataFrame(rows)
    bucket_series = pd.Series(
        [entry["time_bucket_volumes"] for entry in bucket_rows],
        index=frame.index,
        dtype=object,
    )
    return frame, bucket_series


def build_climate_day_table(market_days: pd.DataFrame) -> pd.DataFrame:
    if market_days.empty:
        return market_days
    market_days = market_days.copy()
    for hours in (1, 3, 6):
        vol_col = f"vol_last_{hours}h"
        if vol_col not in market_days.columns:
            market_days[vol_col] = (
                market_days[f"share_vol_last_{hours}h"] * market_days["contracts_traded"]
            )
    mid_band = market_days[market_days["band"] == "10_90"]
    bracket_volumes = (
        mid_band[mid_band["contracts_traded"] > 0]
        .groupby(["climate_date", "season", "era", "ticker"], as_index=False)["contracts_traded"]
        .sum()
    )
    concentration = bracket_volumes.groupby(["climate_date", "season", "era"], as_index=False).agg(
        n_brackets_with_volume=("ticker", "nunique"),
        top_bracket_share=(
            "contracts_traded",
            lambda s: float(s.max() / s.sum()) if s.sum() else float("nan"),
        ),
    )
    grouped = market_days.groupby(
        ["climate_date", "season", "era", "band"],
        as_index=False,
    ).agg(
        premium_traded_climate_day=("premium_traded", "sum"),
        contracts_traded_climate_day=("contracts_traded", "sum"),
        n_brackets=("ticker", "nunique"),
        vol_last_1h=("vol_last_1h", "sum"),
        vol_last_3h=("vol_last_3h", "sum"),
        vol_last_6h=("vol_last_6h", "sum"),
    )
    grouped["share_vol_last_1h"] = grouped["vol_last_1h"] / grouped[
        "contracts_traded_climate_day"
    ].replace(0, float("nan"))
    grouped["share_vol_last_3h"] = grouped["vol_last_3h"] / grouped[
        "contracts_traded_climate_day"
    ].replace(0, float("nan"))
    grouped["share_vol_last_6h"] = grouped["vol_last_6h"] / grouped[
        "contracts_traded_climate_day"
    ].replace(0, float("nan"))
    grouped = grouped.merge(
        concentration,
        on=["climate_date", "season", "era"],
        how="left",
    )
    grouped["n_brackets_with_volume"] = grouped["n_brackets_with_volume"].fillna(0).astype(int)
    return grouped


def _pooled_share(frame: pd.DataFrame, hours: int) -> float:
    """Σ vol_last_Nh / Σ contracts. Zero-volume days contribute 0 to both sums."""
    if frame.empty:
        return float("nan")
    vol_col = f"vol_last_{hours}h"
    if vol_col in frame.columns:
        vol = frame[vol_col].fillna(0.0)
    else:
        vol = (frame[f"share_vol_last_{hours}h"] * frame["contracts_traded"]).fillna(0.0)
    contracts = float(frame["contracts_traded"].sum())
    if contracts <= 0:
        return float("nan")
    return float(vol.sum() / contracts)


def _median_share_active(frame: pd.DataFrame, hours: int, *, min_contracts: float) -> float:
    active = frame[frame["contracts_traded"] >= min_contracts]
    if active.empty:
        return float("nan")
    return float(active[f"share_vol_last_{hours}h"].median())


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
    for band in BAND_NAMES:
        md = market_days[market_days["band"] == band]
        cd = climate_days[climate_days["band"] == band]
        for era in sorted(md["era"].unique()):
            for season in SEASONS:
                md_slice = md[(md["era"] == era) & (md["season"] == season)]
                cd_slice = cd[(cd["era"] == era) & (cd["season"] == season)]
                if md_slice.empty and cd_slice.empty:
                    continue
                contracts_dist = _distribution_stats(md_slice["contracts_traded"])
                premium_dist = _distribution_stats(md_slice["premium_traded"])
                climate_premium_dist = _distribution_stats(cd_slice["premium_traded_climate_day"])
                climate_contracts_dist = _distribution_stats(
                    cd_slice["contracts_traded_climate_day"]
                )
                zero_frac = (
                    float((md_slice["contracts_traded"] <= 0).mean())
                    if len(md_slice)
                    else float("nan")
                )
                if band == "10_90" and "top_bracket_share" in cd_slice.columns:
                    top_share_dist = _distribution_stats(cd_slice["top_bracket_share"])
                    brackets_dist = _distribution_stats(cd_slice["n_brackets_with_volume"])
                else:
                    top_share_dist = _distribution_stats(pd.Series(dtype=float))
                    brackets_dist = _distribution_stats(pd.Series(dtype=float))
                rows.append(
                    {
                        "era": era,
                        "season": season,
                        "band": band,
                        "n_market_days": int(len(md_slice)),
                        "n_climate_days": int(cd_slice["climate_date"].nunique()),
                        "zero_volume_market_day_frac": zero_frac,
                        "median_contracts_market_day": contracts_dist["median"],
                        "p25_contracts_market_day": contracts_dist["p25"],
                        "p75_contracts_market_day": contracts_dist["p75"],
                        "median_premium_market_day": premium_dist["median"],
                        "p25_premium_market_day": premium_dist["p25"],
                        "p75_premium_market_day": premium_dist["p75"],
                        "median_contracts_climate_day": climate_contracts_dist["median"],
                        "p25_contracts_climate_day": climate_contracts_dist["p25"],
                        "p75_contracts_climate_day": climate_contracts_dist["p75"],
                        "median_premium_climate_day": climate_premium_dist["median"],
                        "p25_premium_climate_day": climate_premium_dist["p25"],
                        "p75_premium_climate_day": climate_premium_dist["p75"],
                        "median_brackets_with_volume": brackets_dist["median"],
                        "p25_brackets_with_volume": brackets_dist["p25"],
                        "p75_brackets_with_volume": brackets_dist["p75"],
                        "median_top_bracket_share": top_share_dist["median"],
                        "p25_top_bracket_share": top_share_dist["p25"],
                        "p75_top_bracket_share": top_share_dist["p75"],
                        "median_share_vol_last_1h": (
                            float(md_slice["share_vol_last_1h"].median())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "median_share_vol_last_3h": (
                            float(md_slice["share_vol_last_3h"].median())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "median_share_vol_last_6h": (
                            float(md_slice["share_vol_last_6h"].median())
                            if len(md_slice)
                            else float("nan")
                        ),
                        "pooled_share_vol_last_1h": _pooled_share(md_slice, 1),
                        "pooled_share_vol_last_3h": _pooled_share(md_slice, 3),
                        "pooled_share_vol_last_6h": _pooled_share(md_slice, 6),
                        "median_share_vol_last_1h_contracts_ge20": _median_share_active(
                            md_slice, 1, min_contracts=20
                        ),
                        "median_share_vol_last_3h_contracts_ge20": _median_share_active(
                            md_slice, 3, min_contracts=20
                        ),
                        "median_share_vol_last_6h_contracts_ge20": _median_share_active(
                            md_slice, 6, min_contracts=20
                        ),
                        **{f"climate_day_premium_{k}": v for k, v in climate_premium_dist.items()},
                    }
                )
    return pd.DataFrame(rows)


def _collect_time_to_close_volumes(
    market_days: pd.DataFrame,
    bucket_series: pd.Series,
) -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for idx, row in market_days.iterrows():
        if row["band"] != "10_90" or row["era"] != "2022_plus":
            continue
        buckets = bucket_series.loc[idx]
        if not isinstance(buckets, dict):
            continue
        for label, volume in buckets.items():
            rows.append({"bucket": label, "volume": volume})
    if not rows:
        return pd.DataFrame(columns=["bucket", "volume"])
    return pd.DataFrame(rows)


def write_outputs(summary: pd.DataFrame, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "turnover_census.csv"
    assert_non_empty_frame(summary, what="turnover_census.csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTE_UPPER_BOUND}\n")
        handle.write(f"# {NOTE_PREMIUM_ONCE}\n")
        handle.write(f"# {NOTE_ERA_SPLIT}\n")
        handle.write(f"# {NOTE_KALSHI_ONLY}\n")
        handle.write(f"# {NOTE_MEDIAN_OF_RATIOS}\n")
    summary.to_csv(csv_path, mode="a", index=False)
    return csv_path


def _plot_volume_by_season(climate_days: pd.DataFrame, path: Path) -> None:
    plot = climate_days[(climate_days["band"] == "10_90") & (climate_days["era"] == "2022_plus")]
    if plot.empty:
        return
    data = [
        plot.loc[plot["season"] == season, "contracts_traded_climate_day"].dropna().values
        for season in SEASONS
    ]
    labels = [s for s, values in zip(SEASONS, data, strict=True) if len(values)]
    data = [values for values in data if len(values)]
    if not data:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.boxplot(data, tick_labels=labels)
    ax.set_xlabel("season")
    ax.set_ylabel("contracts traded per climate day")
    ax.set_title("Volume by season (10-90c band, 2022+)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _plot_volume_vs_time_to_close(time_volumes: pd.DataFrame, path: Path) -> None:
    if time_volumes.empty:
        return
    order = ["0-1h", "1-3h", "3-6h", "6-12h", "12-24h", "24-48h", "48h+"]
    grouped = time_volumes.groupby("bucket", as_index=False)["volume"].sum()
    total = grouped["volume"].sum()
    if total <= 0:
        return
    grouped["share"] = grouped["volume"] / total
    grouped["bucket"] = pd.Categorical(grouped["bucket"], categories=order, ordered=True)
    grouped = grouped.sort_values("bucket")
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(grouped["bucket"].astype(str), grouped["share"])
    ax.set_xlabel("hours to close")
    ax.set_ylabel("share of contract volume")
    ax.set_title("Volume vs time-to-close (10-90c band, 2022+)")
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _plot_volume_by_price_region(climate_days: pd.DataFrame, path: Path) -> None:
    mid = climate_days[(climate_days["band"] == "10_90") & (climate_days["era"] == "2022_plus")][
        ["climate_date", "season", "contracts_traded_climate_day"]
    ]
    tails = climate_days[(climate_days["band"] == "tails") & (climate_days["era"] == "2022_plus")][
        ["climate_date", "season", "contracts_traded_climate_day"]
    ]
    if mid.empty and tails.empty:
        return
    merged = mid.merge(
        tails,
        on=["climate_date", "season"],
        how="outer",
        suffixes=("_10_90", "_tails"),
    ).fillna(0.0)
    merged["share_10_90"] = merged["contracts_traded_climate_day_10_90"] / (
        merged["contracts_traded_climate_day_10_90"] + merged["contracts_traded_climate_day_tails"]
    ).replace(0, float("nan"))
    fig, ax = plt.subplots(figsize=(8, 4))
    data = [
        merged.loc[merged["season"] == season, "share_10_90"].dropna().values for season in SEASONS
    ]
    labels = [s for s, values in zip(SEASONS, data, strict=True) if len(values)]
    data = [values for values in data if len(values)]
    if not data:
        return
    ax.boxplot(data, tick_labels=labels)
    ax.set_xlabel("season")
    ax.set_ylabel("share of volume in 10-90c band")
    ax.set_title("Volume by price region (2022+)")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def _print_headline(
    market_days: pd.DataFrame,
    climate_days: pd.DataFrame,
    summary: pd.DataFrame,
) -> None:
    md = market_days[(market_days["band"] == "10_90") & (market_days["era"] == "2022_plus")]
    cd = climate_days[(climate_days["band"] == "10_90") & (climate_days["era"] == "2022_plus")]
    if md.empty:
        print("no turnover rows in 10-90c band for 2022+")
        return
    zero_frac = float((md["contracts_traded"] <= 0).mean())
    print("\n=== headline (2022_plus, 10_90 band, market-day medians) ===")
    print(
        f"median_contracts_per_market_day={md['contracts_traded'].median():.2f} "
        f"median_premium_per_market_day={md['premium_traded'].median():.2f} "
        f"median_brackets_with_volume_per_climate_day="
        f"{cd['n_brackets_with_volume'].median():.2f} "
        f"zero_volume_market_day_frac={zero_frac:.3f}"
    )
    primary = summary[(summary["band"] == "10_90") & (summary["era"] == "2022_plus")]
    if len(primary) > 1:
        print("\n--- by season ---")
        for season_row in primary.sort_values("season").itertuples(index=False):
            print(
                f"season={season_row.season} "
                f"median_contracts_per_market_day={season_row.median_contracts_market_day:.2f} "
                f"median_premium_per_market_day={season_row.median_premium_market_day:.2f} "
                f"median_brackets_with_volume_per_climate_day="
                f"{season_row.median_brackets_with_volume:.2f} "
                f"zero_volume_market_day_frac={season_row.zero_volume_market_day_frac:.3f}"
            )


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    markets = load_markets(raw_dir)

    print("Turnover census: measurement only (no Gate 0 arithmetic)")
    print(NOTE_UPPER_BOUND)
    print(NOTE_PREMIUM_ONCE)
    print(NOTE_ERA_SPLIT)
    print(NOTE_KALSHI_ONLY)
    print(NOTE_MEDIAN_OF_RATIOS)

    if not markets:
        print("no markets_history data in raw_dir")
        return 0

    market_days, bucket_series = build_market_day_table(raw_dir, markets)
    climate_days = build_climate_day_table(market_days)
    summary = summarize_turnover(market_days, climate_days)
    time_volumes = _collect_time_to_close_volumes(market_days, bucket_series)

    csv_path = write_outputs(summary, out_dir)
    season_png = out_dir / "turnover_census_volume_by_season.png"
    ttc_png = out_dir / "turnover_census_volume_vs_time_to_close.png"
    region_png = out_dir / "turnover_census_volume_by_price_region.png"
    _plot_volume_by_season(climate_days, season_png)
    _plot_volume_vs_time_to_close(time_volumes, ttc_png)
    _plot_volume_by_price_region(climate_days, region_png)

    print(f"\nsummary csv: {csv_path}")
    print(f"volume by season: {season_png}")
    print(f"volume vs time-to-close: {ttc_png}")
    print(f"volume by price region: {region_png}")
    _print_headline(market_days, climate_days, summary)

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
