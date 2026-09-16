"""99-level vs 9-decile interpolation, plus neighbor-cell P50 sensitivity.

Reads:
  data/nbm/decoded_v441          (9 deciles, 300-day sample)
  data/nbm/decoded_v441_p99      (P1-P99 + value_f_n/s/w/e on a 50-day subset)
  analysis/out/k2_t24_tradable.csv
  CLINYC + hourly ASOS (window-restricted max)

Does not open backfill sqlite. Measurement only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.forecast_vs_market import (
    bracket_probabilities_for_markets,
    load_nbm_ladder,
    markets_for_climate_date,
)
from analysis.k2_diagnostics import pit_row
from analysis.k2_rigor import window_pit_table
from analysis.murphy import paired_brier_diff
from analysis.spread_census import load_markets, season_of, select_full_day_labels
from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import climate_date_of
from ingestion.config_loader import load_config
from ingestion.nbm_archive import DEFAULT_PERCENTILE_LEVELS

logger = logging.getLogger(__name__)

DECILE_LEVELS = tuple(DEFAULT_PERCENTILE_LEVELS)
NEIGHBOR_COLS = ("value_f_n", "value_f_s", "value_f_w", "value_f_e")


def subsample_levels(
    ladder: pd.DataFrame,
    levels: tuple[int, ...] = DECILE_LEVELS,
) -> pd.DataFrame:
    return ladder[ladder["percentile_level"].isin(levels)].copy()


def p_at(ladder: pd.DataFrame, level: int, col: str = "value_f") -> float | None:
    if col not in ladder.columns:
        return None
    match = ladder.loc[ladder["percentile_level"] == level, col]
    if match.empty:
        return None
    value = match.iloc[0]
    if pd.isna(value):
        return None
    return float(value)


def tail_coverage(ladder: pd.DataFrame, high_f: float | None) -> dict[str, Any]:
    """P1-P99 coverage; independent of the 9-decile P10-P90 PIT."""
    p1 = p_at(ladder, 1)
    p99 = p_at(ladder, 99)
    out: dict[str, Any] = {
        "nbm_p1_f": p1,
        "nbm_p99_f": p99,
        "below_p1": None,
        "in_p1_p99": None,
        "above_p99": None,
    }
    if high_f is None or p1 is None or p99 is None:
        return out
    high = float(high_f)
    out["below_p1"] = high < p1
    out["in_p1_p99"] = p1 <= high <= p99
    out["above_p99"] = high > p99
    return out


def neighbor_p50_errors(
    ladder: pd.DataFrame,
    observed_f: float | None,
) -> dict[str, Any]:
    nearest = p_at(ladder, 50, "value_f")
    row: dict[str, Any] = {"p50_nearest": nearest}
    neighbor_vals: list[float] = []
    for col in NEIGHBOR_COLS:
        value = p_at(ladder, 50, col)
        row[f"p50_{col.removeprefix('value_f_')}"] = value
        if value is not None:
            neighbor_vals.append(value)
    row["p50_neighbor_mean"] = (
        float(sum(neighbor_vals) / len(neighbor_vals)) if neighbor_vals else None
    )
    if observed_f is None or nearest is None:
        row["nearest_abs_err"] = None
        row["best_neighbor_abs_err"] = None
        row["neighbor_closer"] = None
        row["mean_cell_abs_err"] = None
        return row
    obs = float(observed_f)
    nearest_err = abs(nearest - obs)
    row["nearest_abs_err"] = nearest_err
    neighbor_errs: list[float] = []
    for col in NEIGHBOR_COLS:
        name = col.removeprefix("value_f_")
        value = row.get(f"p50_{name}")
        if value is None:
            row[f"{name}_abs_err"] = None
            continue
        err = abs(float(value) - obs)
        row[f"{name}_abs_err"] = err
        neighbor_errs.append(err)
    best = min(neighbor_errs) if neighbor_errs else None
    row["best_neighbor_abs_err"] = best
    row["neighbor_closer"] = bool(best is not None and best + 1e-9 < nearest_err)
    mean_cell = row.get("p50_neighbor_mean")
    row["mean_cell_abs_err"] = abs(float(mean_cell) - obs) if mean_cell is not None else None
    return row


def _prob_mae(left: dict[str, float], right: dict[str, float]) -> tuple[float | None, int]:
    keys = set(left) & set(right)
    if not keys:
        return None, 0
    mae = sum(abs(left[k] - right[k]) for k in keys) / len(keys)
    return mae, len(keys)


def run(
    config: dict[str, Any],
    out_dir: Path,
    p99_dir: Path,
    t24_dir: Path,
) -> int:
    storage = config.get("storage") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    if not p99_dir.exists():
        print(f"missing 99-level dir {p99_dir} — run nbm_archive --percentile-levels 1-99")
        return 1

    p99_days = sorted(path.stem for path in p99_dir.glob("*.parquet"))
    trad_path = out_dir / "k2_t24_tradable.csv"
    pit_path = out_dir / "k2_pit_days.csv"
    tradable = pd.read_csv(trad_path) if trad_path.exists() else pd.DataFrame()
    pit = pd.read_csv(pit_path) if pit_path.exists() else pd.DataFrame()
    all_markets = load_markets(raw_dir)
    asos = load_asos_observations_from_raw(raw_dir, station="NYC")
    asos_by_day: dict[str, list[Any]] = {}
    for obs in asos:
        asos_by_day.setdefault(climate_date_of(obs.valid_utc).isoformat(), []).append(obs)
    cli_by_day: dict[str, float] = {}
    if not pit.empty:
        for row in pit.itertuples(index=False):
            if pd.notna(row.cli_high_f):
                cli_by_day[str(row.climate_date)] = float(row.cli_high_f)
    else:
        labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
        if labels_csv.exists():
            labels = select_full_day_labels(labels_csv)
            for row in labels.itertuples(index=False):
                if pd.notna(row.high_F):
                    cli_by_day[str(row.climate_date)] = float(row.high_F)

    day_rows: list[dict[str, Any]] = []
    bracket_rows: list[dict[str, Any]] = []
    n_overlap = 0
    for climate_date in p99_days:
        ladder99 = load_nbm_ladder(p99_dir, climate_date)
        ladder9 = load_nbm_ladder(t24_dir, climate_date)
        if ladder99 is None or ladder9 is None:
            continue
        n_overlap += 1
        day_markets = markets_for_climate_date(all_markets, climate_date)
        probs9 = bracket_probabilities_for_markets(ladder9, day_markets)
        probs99 = bracket_probabilities_for_markets(ladder99, day_markets)
        probs_sub = bracket_probabilities_for_markets(subsample_levels(ladder99), day_markets)
        mae_sub, n_sub = _prob_mae(probs9, probs_sub)
        mae_99, n_99 = _prob_mae(probs9, probs99)
        cli_high = cli_by_day.get(climate_date)
        pit9 = pit_row(ladder9, cli_high)
        pit99 = pit_row(ladder99, cli_high)
        tails = tail_coverage(ladder99, cli_high)
        neighbors = neighbor_p50_errors(ladder99, cli_high)
        day_rows.append(
            {
                "climate_date": climate_date,
                "season": season_of(climate_date),
                "n_levels_p99": int(ladder99["percentile_level"].nunique()),
                "mae_decile_vs_subsample": mae_sub,
                "n_brackets_subsample": n_sub,
                "mae_decile_vs_p99": mae_99,
                "n_brackets_p99": n_99,
                "cli_high_f": cli_high,
                "cli_in_p10_p90_decile": pit9.get("in_p10_p90"),
                "cli_in_p10_p90_p99": pit99.get("in_p10_p90"),
                "cli_in_p1_p99": tails.get("in_p1_p99"),
                "cli_below_p1": tails.get("below_p1"),
                "cli_above_p99": tails.get("above_p99"),
                **{
                    f"nbm9_{k}": v
                    for k, v in pit9.items()
                    if k in ("nbm_p10_f", "nbm_p50_f", "nbm_p90_f")
                },
                **neighbors,
            }
        )
        day_trad = (
            tradable[tradable["climate_date"].astype(str) == climate_date]
            if not tradable.empty
            else pd.DataFrame()
        )
        for _, snap in day_trad.iterrows():
            ticker = str(snap["ticker"])
            if ticker not in probs99:
                continue
            bracket_rows.append(
                {
                    "climate_date": climate_date,
                    "season": snap.get("season") or season_of(climate_date),
                    "ticker": ticker,
                    "nbm_prob_decile": snap["nbm_prob"],
                    "nbm_prob_subsample": probs_sub.get(ticker),
                    "nbm_prob_p99": probs99.get(ticker),
                    "market_mid": snap["market_mid_carryforward"],
                    "settled_yes": snap["settled_yes"],
                    "in_primary_band": snap.get("in_primary_band"),
                }
            )

    days = pd.DataFrame(day_rows)
    days_path = out_dir / "k2_p99_days.csv"
    days.to_csv(days_path, index=False)
    print(f"p99_days={len(p99_days)} overlap_with_v441={n_overlap} wrote {days_path}")
    brackets = pd.DataFrame(bracket_rows)
    br_path = out_dir / "k2_p99_brackets.csv"
    brackets.to_csv(br_path, index=False)

    summary: list[dict[str, Any]] = []
    if not days.empty:
        print(
            f"median_|decile-subsample|={days['mae_decile_vs_subsample'].median():.6f} "
            f"median_|decile-p99|={days['mae_decile_vs_p99'].median():.6f}"
        )
        summary.append(
            {
                "metric": "median_mae_decile_vs_subsample",
                "value": float(days["mae_decile_vs_subsample"].median()),
            }
        )
        summary.append(
            {
                "metric": "median_mae_decile_vs_p99",
                "value": float(days["mae_decile_vs_p99"].median()),
            }
        )
        if days["cli_in_p1_p99"].notna().any():
            summary.append(
                {
                    "metric": "cli_in_p1_p99",
                    "value": float(days["cli_in_p1_p99"].mean()),
                }
            )
            summary.append(
                {
                    "metric": "cli_below_p1",
                    "value": float(days["cli_below_p1"].mean()),
                }
            )
            summary.append(
                {
                    "metric": "cli_above_p99",
                    "value": float(days["cli_above_p99"].mean()),
                }
            )
        if days["neighbor_closer"].notna().any():
            summary.append(
                {
                    "metric": "frac_neighbor_closer_to_cli",
                    "value": float(days["neighbor_closer"].mean()),
                }
            )
            summary.append(
                {
                    "metric": "median_nearest_abs_err",
                    "value": float(days["nearest_abs_err"].median()),
                }
            )
            summary.append(
                {
                    "metric": "median_best_neighbor_abs_err",
                    "value": float(days["best_neighbor_abs_err"].median()),
                }
            )
            print(
                f"neighbor_closer_to_cli={days['neighbor_closer'].mean():.3f} "
                f"median_nearest_abs_err={days['nearest_abs_err'].median():.3f} "
                f"median_best_neighbor={days['best_neighbor_abs_err'].median():.3f}"
            )
            for name in ("n", "s", "w", "e"):
                col = f"{name}_abs_err"
                if col in days.columns and days[col].notna().any():
                    med = float(days[col].median())
                    summary.append({"metric": f"median_{name}_abs_err", "value": med})
                    print(f"median_{name}_abs_err={med:.3f}")
            if "mean_cell_abs_err" in days.columns and days["mean_cell_abs_err"].notna().any():
                med = float(days["mean_cell_abs_err"].median())
                summary.append({"metric": "median_mean_cell_abs_err", "value": med})
                print(f"median_mean_cell_abs_err={med:.3f}")

    if not pit.empty:
        pit_sub = pit[pit["climate_date"].astype(str).isin(p99_days)]
        window = window_pit_table(pit=pit_sub, decoded_dir=p99_dir, asos_by_day=asos_by_day)
        if not window.empty:
            win_path = out_dir / "k2_p99_window_pit.csv"
            window.to_csv(win_path, index=False)
            print(
                f"p99 window PIT n={len(window)} "
                f"in_p10_p90={window['win_in_p10_p90'].mean():.3f}"
            )

    if not brackets.empty:
        band = brackets["in_primary_band"].fillna(False).astype(bool)
        primary = brackets[band] if band.any() else brackets
        usable = primary[
            primary["nbm_prob_decile"].notna()
            & primary["nbm_prob_p99"].notna()
            & primary["settled_yes"].notna()
        ]
        if not usable.empty:
            yes = usable["settled_yes"].astype(float).to_numpy()
            d9 = usable["nbm_prob_decile"].astype(float).to_numpy()
            d99 = usable["nbm_prob_p99"].astype(float).to_numpy()
            diff = paired_brier_diff(yes, d9, d99)
            print(
                f"primary Brier decile vs p99 n={len(usable)} "
                f"decile={diff['brier_a']:.4f} p99={diff['brier_b']:.4f} "
                f"9-99={diff['mean']:+.4f} "
                f"95%CI=[{diff['ci_lo']:+.4f},{diff['ci_hi']:+.4f}]"
            )
            summary.append({"metric": "brier_decile", "value": diff["brier_a"]})
            summary.append({"metric": "brier_p99", "value": diff["brier_b"]})
            summary.append({"metric": "brier_decile_minus_p99", "value": diff["mean"]})

    pd.DataFrame(summary).to_csv(out_dir / "k2_p99_summary.csv", index=False)
    return 0 if n_overlap else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="99-level vs 9-decile and neighbor P50")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    parser.add_argument("--p99-dir", type=Path, default=Path("data/nbm/decoded_v441_p99"))
    parser.add_argument("--t24-dir", type=Path, default=Path("data/nbm/decoded_v441"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(load_config(args.config), args.out_dir, args.p99_dir, args.t24_dir)


if __name__ == "__main__":
    raise SystemExit(main())
