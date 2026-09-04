"""NBM forecast vs Kalshi market comparison at T-24h (Session 6c).

Allowed DB: none. Reads decoded NBM parquet, raw JSONL candles/markets, and clinyc.csv.

Measurement only — distributions of forecast-implied bracket probabilities vs market
carry-forward mids at T-24h on the 300-day NBM sample. No pass/fail verdict.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analysis.bracket_enumeration import ParsedStrike, parse_market_strike
from analysis.spread_census import (
    build_snapshot_table,
    era_of,
    load_candles,
    load_markets,
    season_of,
    select_full_day_labels,
    ticker_climate_date,
)
from ingestion.climate_time import load_cli_time_convention
from ingestion.config_loader import load_config
from ingestion.nbm_archive import NbmArchiveBackfill

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

NOTE_NBM_WINDOW = (
    "NOTE: NBM qmd max-window product covers 12Z-06Z(+1), not the full CLI climate day. "
    "Structural mismatch is measured in window_mismatch_k2.csv — carry as bias, not noise."
)
NOTE_DECILE_INTERP = (
    "NOTE: bracket probabilities interpolate across 9 deciles (P10-P90), not the full "
    "99-level ladder. Tail probabilities are extrapolated from the outer knots."
)
NOTE_CARRYFORWARD = (
    "NOTE: market_mid_carryforward is the K1 primary quote rule (no 15-minute staleness "
    "filter). edge_cf = market_mid_carryforward - nbm_prob."
)
NOTE_SAMPLE = (
    "NOTE: restricted to the 300-day stratified NBM sample (seed 43, start 2022-12-11). "
    "Do not pool with void 60-min / f030 backfill at data/nbm/decoded/."
)
NOTE_LATENCY_HEAD = (
    "NOTE: nbm_latency_check HEAD mirror lag can hard-stop at p90>441 even when empirical "
    "vintage (availability-watch settled) passed V1 at idx-confirmed publication times."
)


def bracket_id(strike: ParsedStrike) -> str:
    if strike.role == "between":
        return f"between_{strike.floor_f}_{strike.cap_f}"
    if strike.role == "less":
        return f"less_{strike.cap_f}"
    return f"greater_{strike.floor_f}"


def settled_yes(high_f: float | int | None, strike: ParsedStrike) -> bool | None:
    """Return whether CLI high_F settles this bracket Yes."""
    if high_f is None or (isinstance(high_f, float) and np.isnan(high_f)):
        return None
    high = int(round(float(high_f)))
    if strike.role == "between":
        if strike.floor_f is None or strike.cap_f is None:
            return None
        return strike.floor_f <= high <= strike.cap_f
    if strike.role == "less":
        if strike.cap_f is None:
            return None
        return high < strike.cap_f
    if strike.floor_f is None:
        return None
    return high > strike.floor_f


def _ladder_knots(
    levels: list[int],
    values: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    if not levels:
        raise ValueError("empty ladder")
    order = np.argsort(levels)
    pct = np.array([levels[i] for i in order], dtype=float) / 100.0
    temps = np.array([values[i] for i in order], dtype=float)
    # Extrapolate one knot below/above so np.interp covers tails.
    low_span = max(temps[1] - temps[0], 1.0) if len(temps) > 1 else 1.0
    high_span = max(temps[-1] - temps[-2], 1.0) if len(temps) > 1 else 1.0
    pct_ext = np.concatenate(([max(0.0, pct[0] - 0.10)], pct, [min(1.0, pct[-1] + 0.10)]))
    temps_ext = np.concatenate(
        ([temps[0] - low_span], temps, [temps[-1] + high_span]),
    )
    return temps_ext, pct_ext


def cdf_at_temperature(levels: list[int], values: list[float], temp_f: float) -> float:
    """P(T <= temp_f) from decile ladder via linear interpolation."""
    temps, pct = _ladder_knots(levels, values)
    return float(np.interp(temp_f, temps, pct, left=0.0, right=1.0))


def bracket_probability(
    strike: ParsedStrike,
    levels: list[int],
    values: list[float],
) -> float:
    """Implied Yes probability for one Kalshi bracket from the NBM ladder."""
    if strike.role == "between":
        if strike.floor_f is None or strike.cap_f is None:
            return 0.0
        upper = strike.cap_f + 0.5
        lower = strike.floor_f - 0.5
        return max(
            0.0,
            cdf_at_temperature(levels, values, upper)
            - cdf_at_temperature(levels, values, lower),
        )
    if strike.role == "less":
        if strike.cap_f is None:
            return 0.0
        return max(0.0, cdf_at_temperature(levels, values, strike.cap_f - 0.5))
    if strike.floor_f is None:
        return 0.0
    return max(0.0, 1.0 - cdf_at_temperature(levels, values, strike.floor_f + 0.5))


def bracket_probabilities_for_markets(
    ladder: pd.DataFrame,
    markets: list[dict[str, Any]],
) -> dict[str, float]:
    """Map ticker -> normalized NBM-implied Yes probability."""
    ordered = ladder.sort_values("percentile_level")
    levels = [int(v) for v in ordered["percentile_level"].tolist()]
    values = [float(v) for v in ordered["value_f"].tolist()]
    raw: dict[str, float] = {}
    for market in markets:
        strike = parse_market_strike(market)
        ticker = str(market.get("ticker") or "")
        if strike is None or not ticker:
            continue
        raw[ticker] = bracket_probability(strike, levels, values)
    total = sum(raw.values())
    if total <= 0:
        return raw
    return {ticker: prob / total for ticker, prob in raw.items()}


def sample_climate_dates(config: dict[str, Any]) -> list[str]:
    return [day.isoformat() for day in NbmArchiveBackfill(config).sampled_climate_dates()]


def load_nbm_ladder(decoded_dir: Path, climate_date: str) -> pd.DataFrame | None:
    path = decoded_dir / f"{climate_date}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def markets_for_climate_date(
    markets: list[dict[str, Any]],
    climate_date: str,
) -> list[dict[str, Any]]:
    return [
        market
        for market in markets
        if ticker_climate_date(str(market.get("ticker") or "")) == climate_date
    ]


def build_comparison_table(
    *,
    config: dict[str, Any],
    climate_dates: list[str],
    horizon_h: int = 24,
    primary_band: tuple[float, float] = (0.10, 0.90),
) -> pd.DataFrame:
    storage = config.get("storage") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    decoded_dir = Path(nbm_cfg.get("decoded_dir") or "data/nbm/decoded_v441")
    climate_set = set(climate_dates)

    all_markets = load_markets(raw_dir)
    markets = [
        market
        for market in all_markets
        if ticker_climate_date(str(market.get("ticker") or "")) in climate_set
    ]
    candles = load_candles(raw_dir)
    labels = select_full_day_labels(labels_csv)
    cli_convention = load_cli_time_convention(config)
    snapshots, _stats = build_snapshot_table(
        markets=markets,
        candles_by_ticker=candles,
        labels=labels,
        cli_time_convention=cli_convention,
    )
    if snapshots.empty:
        return pd.DataFrame()
    t24 = snapshots[snapshots["horizon_h"] == horizon_h].copy()
    band_lo, band_hi = primary_band

    rows: list[dict[str, Any]] = []
    for climate_date in sorted(climate_dates):
        ladder = load_nbm_ladder(decoded_dir, climate_date)
        day_markets = markets_for_climate_date(markets, climate_date)
        if ladder is None or not day_markets:
            continue
        probs = bracket_probabilities_for_markets(ladder, day_markets)
        ladder_row = ladder.iloc[0]
        day_snaps = t24[t24["climate_date"] == climate_date]
        for market in day_markets:
            ticker = str(market.get("ticker") or "")
            strike = parse_market_strike(market)
            if strike is None or ticker not in probs:
                continue
            snap = day_snaps[day_snaps["ticker"] == ticker]
            snap_row = snap.iloc[0] if len(snap) else None
            mid_cf = snap_row["mid_carryforward"] if snap_row is not None else None
            in_band = (
                bool(snap_row["two_sided_carryforward"])
                and mid_cf is not None
                and band_lo <= float(mid_cf) <= band_hi
                if snap_row is not None
                else False
            )
            high_f = snap_row["high_F"] if snap_row is not None else None
            rows.append(
                {
                    "climate_date": climate_date,
                    "season": season_of(climate_date),
                    "era": era_of(climate_date),
                    "ticker": ticker,
                    "bracket_id": bracket_id(strike),
                    "strike_role": strike.role,
                    "floor_f": strike.floor_f,
                    "cap_f": strike.cap_f,
                    "nbm_prob": probs[ticker],
                    "market_mid_carryforward": mid_cf,
                    "edge_cf": (
                        float(mid_cf) - probs[ticker] if mid_cf is not None else None
                    ),
                    "two_sided_carryforward": (
                        bool(snap_row["two_sided_carryforward"]) if snap_row is not None else False
                    ),
                    "in_primary_band": in_band,
                    "quote_present": (
                        bool(snap_row["quote_present"]) if snap_row is not None else False
                    ),
                    "in_trading_window": (
                        bool(snap_row["in_trading_window"]) if snap_row is not None else False
                    ),
                    "settled_yes": settled_yes(high_f, strike),
                    "high_F": high_f,
                    "forecast_hour": int(ladder_row["forecast_hour"]),
                    "vintage_cycle_utc": ladder_row["vintage_cycle_utc"],
                    "snapshot_margin_min_p90": ladder_row.get("snapshot_margin_min_p90"),
                }
            )
    return pd.DataFrame(rows)


def summarize_comparison(comparison: pd.DataFrame) -> pd.DataFrame:
    if comparison.empty:
        return comparison
    primary = comparison[comparison["in_primary_band"]].copy()
    rows: list[dict[str, Any]] = []
    for (season, era), group in primary.groupby(["season", "era"], dropna=False):
        edge = group["edge_cf"].dropna()
        brier = (
            ((group["nbm_prob"] - group["settled_yes"].astype(float)) ** 2).mean()
            if group["settled_yes"].notna().any()
            else None
        )
        market_brier = (
            ((group["market_mid_carryforward"] - group["settled_yes"].astype(float)) ** 2).mean()
            if group["settled_yes"].notna().any()
            else None
        )
        rows.append(
            {
                "season": season,
                "era": era,
                "n_obs": int(len(group)),
                "n_days": int(group["climate_date"].nunique()),
                "edge_cf_median": float(edge.median()) if len(edge) else None,
                "edge_cf_mean": float(edge.mean()) if len(edge) else None,
                "abs_edge_cf_median": float(edge.abs().median()) if len(edge) else None,
                "nbm_brier": float(brier) if brier is not None else None,
                "market_brier": float(market_brier) if market_brier is not None else None,
                "two_sided_share": float(group["two_sided_carryforward"].mean()),
            }
        )
    overall_edge = primary["edge_cf"].dropna()
    rows.append(
        {
            "season": "ALL",
            "era": "ALL",
            "n_obs": int(len(primary)),
            "n_days": int(primary["climate_date"].nunique()),
            "edge_cf_median": float(overall_edge.median()) if len(overall_edge) else None,
            "edge_cf_mean": float(overall_edge.mean()) if len(overall_edge) else None,
            "abs_edge_cf_median": float(overall_edge.abs().median()) if len(overall_edge) else None,
            "nbm_brier": float(
                ((primary["nbm_prob"] - primary["settled_yes"].astype(float)) ** 2).mean()
            )
            if primary["settled_yes"].notna().any()
            else None,
            "market_brier": float(
                (
                    (primary["market_mid_carryforward"] - primary["settled_yes"].astype(float))
                    ** 2
                ).mean()
            )
            if primary["settled_yes"].notna().any()
            else None,
            "two_sided_share": float(primary["two_sided_carryforward"].mean()),
        }
    )
    return pd.DataFrame(rows)


def _calibration_bins(
    comparison: pd.DataFrame,
    *,
    prob_col: str,
    n_bins: int = 10,
) -> pd.DataFrame:
    primary = comparison[comparison["in_primary_band"] & comparison["settled_yes"].notna()].copy()
    if primary.empty:
        return primary
    primary["bin"] = pd.cut(primary[prob_col], bins=n_bins, labels=False, include_lowest=True)
    rows: list[dict[str, Any]] = []
    for bin_id, group in primary.groupby("bin", dropna=True):
        rows.append(
            {
                "bin": int(bin_id),
                "prob_mean": float(group[prob_col].mean()),
                "outcome_rate": float(group["settled_yes"].astype(float).mean()),
                "n": int(len(group)),
            }
        )
    return pd.DataFrame(rows)


def write_figures(
    comparison: pd.DataFrame,
    out_dir: Path,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if comparison.empty or "in_primary_band" not in comparison.columns:
        return written
    primary = comparison[comparison["in_primary_band"]].copy()

    if not primary.empty:
        fig, ax = plt.subplots(figsize=(8, 5))
        edge = primary["edge_cf"].dropna()
        ax.hist(edge, bins=40, color="#4c72b0", edgecolor="white")
        ax.axvline(0.0, color="black", linewidth=1, linestyle="--")
        ax.set_xlabel("market_mid_carryforward - nbm_prob")
        ax.set_ylabel("count")
        ax.set_title("T-24h edge distribution (primary 10-90¢ band)")
        path = out_dir / "forecast_vs_market_edge_hist.png"
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

        fig, ax = plt.subplots(figsize=(8, 5))
        primary.boxplot(column="edge_cf", by="season", ax=ax)
        ax.axhline(0.0, color="black", linewidth=1, linestyle="--")
        ax.set_xlabel("season")
        ax.set_ylabel("edge_cf")
        ax.set_title("T-24h edge by season (primary band)")
        plt.suptitle("")
        path = out_dir / "forecast_vs_market_edge_by_season.png"
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

        cal = _calibration_bins(primary, prob_col="nbm_prob")
        if not cal.empty:
            fig, ax = plt.subplots(figsize=(6, 6))
            ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfect")
            ax.scatter(cal["prob_mean"], cal["outcome_rate"], s=cal["n"] * 2, alpha=0.8)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xlabel("mean nbm_prob (bin)")
            ax.set_ylabel("settled_yes rate")
            ax.set_title("NBM bracket calibration (primary band)")
            path = out_dir / "forecast_vs_market_calibration.png"
            fig.tight_layout()
            fig.savefig(path, dpi=150)
            plt.close(fig)
            written.append(path)

    return written


def run(config: dict[str, Any], out_dir: Path) -> int:
    cfg = config.get("forecast_vs_market") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    horizon_h = int(cfg.get("horizon_h") or nbm_cfg.get("snapshot_horizon_h") or 24)
    band = cfg.get("primary_band") or [0.10, 0.90]
    primary_band = (float(band[0]), float(band[1]))

    climate_dates = sample_climate_dates(config)
    comparison = build_comparison_table(
        config=config,
        climate_dates=climate_dates,
        horizon_h=horizon_h,
        primary_band=primary_band,
    )
    summary = summarize_comparison(comparison)

    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_dir / "forecast_vs_market.csv"
    summary_path = out_dir / "forecast_vs_market_summary.csv"
    comparison.to_csv(detail_path, index=False)
    summary.to_csv(summary_path, index=False)
    figures = write_figures(comparison, out_dir)

    print(f"climate_dates_requested={len(climate_dates)}")
    print(f"comparison_rows={len(comparison)}")
    if len(comparison):
        print(f"primary_band_obs={int(comparison['in_primary_band'].sum())}")
    print(f"wrote {detail_path}")
    print(f"wrote {summary_path}")
    for path in figures:
        print(f"wrote {path}")
    if not summary.empty:
        print("\n=== summary (primary 10-90¢ band) ===")
        print(summary.to_string(index=False))
    print(f"\n{NOTE_NBM_WINDOW}")
    print(NOTE_DECILE_INTERP)
    print(NOTE_CARRYFORWARD)
    print(NOTE_SAMPLE)
    print(NOTE_LATENCY_HEAD)
    return 0 if len(comparison) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBM forecast vs Kalshi market at T-24h")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    cfg = config.get("forecast_vs_market") or {}
    out_dir = args.out_dir or Path(cfg.get("out_dir") or "analysis/out")
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
