"""Deeper Session 6c / K2 cuts on already-computed comparison rows.

Allowed DB: none. Reads analysis/out CSVs, decoded_v441 parquet, clinyc + ASOS.

Measurement only — no pass/fail verdict, no trading rule.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.spread_census import select_full_day_labels
from analysis.window_mismatch import build_k2_day_rows
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)

SUB_ERA_SPLIT = "2024-05-15"
NOTE = (
    "NOTE: slices reuse forecast_vs_market.csv primary-band rows. "
    "Window flags are CLI/ASOS vs NBM 12Z-06Z, both clock hypotheses, no pick. "
    "NBM P50 is the 50th percentile of the 9-decile max-window ladder, not a "
    "point forecast of the CLI high."
)


def _metrics(group: pd.DataFrame, slice_name: str) -> dict[str, Any]:
    if group.empty:
        return {
            "slice": slice_name,
            "n_obs": 0,
            "n_days": 0,
            "edge_cf_median": None,
            "edge_cf_mean": None,
            "abs_edge_cf_median": None,
            "nbm_brier": None,
            "market_brier": None,
            "brier_gap_market_minus_nbm": None,
            "nbm_mae_p50": None,
        }
    settled = group["settled_yes"].notna()
    edge = group["edge_cf"].dropna()
    nbm_brier = (
        float(
            (
                (group.loc[settled, "nbm_prob"] - group.loc[settled, "settled_yes"].astype(float))
                ** 2
            ).mean()
        )
        if settled.any()
        else None
    )
    market_brier = (
        float(
            (
                (
                    group.loc[settled, "market_mid_carryforward"]
                    - group.loc[settled, "settled_yes"].astype(float)
                )
                ** 2
            ).mean()
        )
        if settled.any()
        else None
    )
    mae = None
    if "nbm_p50_f" in group.columns and "high_F" in group.columns:
        pair = group.dropna(subset=["nbm_p50_f", "high_F"]).drop_duplicates("climate_date")
        if not pair.empty:
            mae = float((pair["nbm_p50_f"] - pair["high_F"]).abs().mean())
    gap = None
    if nbm_brier is not None and market_brier is not None:
        gap = float(market_brier - nbm_brier)
    return {
        "slice": slice_name,
        "n_obs": int(len(group)),
        "n_days": int(group["climate_date"].nunique()),
        "edge_cf_median": float(edge.median()) if len(edge) else None,
        "edge_cf_mean": float(edge.mean()) if len(edge) else None,
        "abs_edge_cf_median": float(edge.abs().median()) if len(edge) else None,
        "nbm_brier": nbm_brier,
        "market_brier": market_brier,
        "brier_gap_market_minus_nbm": gap,
        "nbm_mae_p50": mae,
    }


def _load_nbm_p50(decoded_dir: Path, climate_dates: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for climate_date in climate_dates:
        path = decoded_dir / f"{climate_date}.parquet"
        if not path.exists():
            continue
        ladder = pd.read_parquet(path)
        ordered = ladder.sort_values("percentile_level")
        p50 = ordered.loc[ordered["percentile_level"] == 50]
        if p50.empty:
            continue
        rows.append(
            {
                "climate_date": climate_date,
                "nbm_p50_f": float(p50.iloc[0]["value_f"]),
                "nbm_p10_f": float(
                    ordered.loc[ordered["percentile_level"] == 10].iloc[0]["value_f"]
                )
                if (ordered["percentile_level"] == 10).any()
                else None,
                "nbm_p90_f": float(
                    ordered.loc[ordered["percentile_level"] == 90].iloc[0]["value_f"]
                )
                if (ordered["percentile_level"] == 90).any()
                else None,
            }
        )
    return pd.DataFrame(rows)


def _calibration(primary: pd.DataFrame, prob_col: str, n_bins: int = 10) -> pd.DataFrame:
    frame = primary[primary["settled_yes"].notna()].copy()
    if frame.empty:
        return frame
    frame["bin"] = pd.cut(frame[prob_col], bins=n_bins, labels=False, include_lowest=True)
    rows: list[dict[str, Any]] = []
    for bin_id, group in frame.groupby("bin", dropna=True):
        rows.append(
            {
                "source": prob_col,
                "bin": int(bin_id),
                "prob_mean": float(group[prob_col].mean()),
                "outcome_rate": float(group["settled_yes"].astype(float).mean()),
                "n": int(len(group)),
            }
        )
    return pd.DataFrame(rows)


def _k1_t24_headline(census_path: Path) -> pd.DataFrame:
    if not census_path.exists():
        return pd.DataFrame()
    census = pd.read_csv(census_path, comment="#")
    t24 = census[
        (census["horizon_h"] == 24) & (census["era"] == "2022_plus") & (census["band"] == "10_90")
    ].copy()
    if t24.empty:
        return t24
    # Collapse seasons: observation-weighted median of per-row medians is not
    # the pooled median. Report the season rows plus an n-weighted mean of
    # median_spread_carryforward as a descriptive index only.
    t24 = t24[t24["median_spread_carryforward"].notna()].copy()
    return t24[
        [
            "season",
            "regime",
            "n_spread_obs_carryforward",
            "median_spread_carryforward",
            "p25_carryforward",
            "p75_carryforward",
            "median_spread_strict15",
            "median_spread_exclnoreconcile",
        ]
    ]


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config.get("storage") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    decoded_dir = Path(nbm_cfg.get("decoded_dir") or "data/nbm/decoded_v441")
    out_dir.mkdir(parents=True, exist_ok=True)

    comparison_path = out_dir / "forecast_vs_market.csv"
    if not comparison_path.exists():
        print("missing analysis/out/forecast_vs_market.csv — run analysis.forecast_vs_market first")
        return 1

    comparison = pd.read_csv(comparison_path)
    primary = comparison[comparison["in_primary_band"]].copy()
    primary["climate_date"] = primary["climate_date"].astype(str)
    primary["sub_era"] = np.where(
        primary["climate_date"] < SUB_ERA_SPLIT,
        "v4_early",
        "v4_late",
    )

    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    k2_days = build_k2_day_rows(labels, raw_dir) if not labels.empty else pd.DataFrame()
    if not k2_days.empty:
        k2_days["climate_date"] = k2_days["climate_date"].astype(str)
        k2_path = out_dir / "window_mismatch_k2_days.csv"
        k2_days.to_csv(k2_path, index=False)
        print(f"wrote {k2_path} rows={len(k2_days)}")
        primary = primary.merge(
            k2_days[
                [
                    "climate_date",
                    "asos_outside_window",
                    "cli_outside_lst",
                    "cli_outside_ldt",
                    "delta_f",
                    "bracket_disagree",
                ]
            ],
            on="climate_date",
            how="left",
        )

    p50 = _load_nbm_p50(decoded_dir, sorted(primary["climate_date"].unique()))
    if not p50.empty:
        primary = primary.merge(p50, on="climate_date", how="left")

    slices: list[dict[str, Any]] = [_metrics(primary, "primary_all")]
    for season, group in primary.groupby("season"):
        slices.append(_metrics(group, f"season={season}"))
    for role, group in primary.groupby("strike_role"):
        slices.append(_metrics(group, f"role={role}"))
    for era, group in primary.groupby("sub_era"):
        slices.append(_metrics(group, f"sub_era={era}"))
    if "cli_outside_lst" in primary.columns:
        slices.append(_metrics(primary[primary["cli_outside_lst"] == True], "cli_outside_lst"))  # noqa: E712
        slices.append(_metrics(primary[primary["cli_outside_lst"] == False], "cli_inside_lst"))  # noqa: E712
        slices.append(_metrics(primary[primary["asos_outside_window"] == True], "asos_outside"))  # noqa: E712
        slices.append(_metrics(primary[primary["asos_outside_window"] == False], "asos_inside"))  # noqa: E712
    if primary["nbm_prob"].notna().any():
        tercile = pd.qcut(
            primary["nbm_prob"], 3, labels=["nbm_low", "nbm_mid", "nbm_high"], duplicates="drop"
        )
        primary["nbm_tercile"] = tercile.astype(str)
        for name, group in primary.groupby("nbm_tercile"):
            slices.append(_metrics(group, f"tercile={name}"))

    slice_frame = pd.DataFrame(slices)
    slice_path = out_dir / "session6c_ext_slices.csv"
    slice_frame.to_csv(slice_path, index=False)
    print(f"wrote {slice_path}")
    print(slice_frame.to_string(index=False))

    cal_nbm = _calibration(primary, "nbm_prob")
    cal_mkt = _calibration(primary, "market_mid_carryforward")
    cal = pd.concat([cal_nbm, cal_mkt], ignore_index=True)
    cal_path = out_dir / "session6c_ext_calibration.csv"
    cal.to_csv(cal_path, index=False)
    print(f"wrote {cal_path}")

    k1 = _k1_t24_headline(out_dir / "spread_census.csv")
    if not k1.empty:
        k1_path = out_dir / "session6c_ext_k1_t24.csv"
        k1.to_csv(k1_path, index=False)
        print(f"wrote {k1_path}")
        print("\n=== K1 T-24h 2022+ 10-90¢ rows with a carry-forward median ===")
        print(k1.to_string(index=False))

    print(f"\n{NOTE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Session 6c extension slices")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    cfg = config.get("forecast_vs_market") or {}
    out_dir = args.out_dir or Path(cfg.get("out_dir") or "analysis/out")
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
