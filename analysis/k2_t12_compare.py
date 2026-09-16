"""Compare frozen T-24h NBM vs a later vintage at T-12h.

Reads decoded_v441 (T-24) and decoded_v441_t12 (T-12 D 00Z f030).
Joins to k2_horizon_books.csv T-12h primary-band rows. No candle reload.

Also reports a paired bootstrap CI on the T-24 tradable Brier gap
(disk-only; independent of how many T-12 ladders are present).

Measurement only.
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
    sample_climate_dates,
)
from analysis.k2_diagnostics import brier_pair
from analysis.murphy import paired_brier_diff
from analysis.spread_census import load_markets, ticker_climate_date
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)


def _finite_pair(frame: pd.DataFrame, left: str, right: str) -> pd.DataFrame:
    return frame[frame[left].notna() & frame[right].notna() & frame["settled_yes"].notna()].copy()


def _print_briers(label: str, frame: pd.DataFrame, nbm_col: str, mid_col: str) -> dict[str, Any]:
    nbm, mkt = brier_pair(frame, nbm_col, mid_col)
    usable = _finite_pair(frame, nbm_col, mid_col)
    row: dict[str, Any] = {
        "slice": label,
        "n": int(len(usable)),
        "nbm_brier": nbm,
        "market_brier": mkt,
    }
    if usable.empty or nbm is None or mkt is None:
        print(f"{label} n={row['n']}")
        return row
    yes = usable["settled_yes"].astype(float).to_numpy()
    nbm_p = usable[nbm_col].astype(float).to_numpy()
    mkt_p = usable[mid_col].astype(float).to_numpy()
    vs_mkt = paired_brier_diff(yes, nbm_p, mkt_p)
    row["nbm_minus_market"] = vs_mkt["mean"]
    row["nbm_minus_market_ci_lo"] = vs_mkt["ci_lo"]
    row["nbm_minus_market_ci_hi"] = vs_mkt["ci_hi"]
    row["frac_nbm_better"] = vs_mkt["frac_a_better"]
    print(
        f"{label} n={row['n']} nbm_brier={nbm:.4f} market_brier={mkt:.4f} "
        f"nbm-market={vs_mkt['mean']:+.4f} "
        f"95%CI=[{vs_mkt['ci_lo']:+.4f},{vs_mkt['ci_hi']:+.4f}] "
        f"frac_nbm_better={vs_mkt['frac_a_better']:.3f}"
    )
    return row


def t24_bootstrap_from_tradable(tradable: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    primary = (
        tradable[tradable["in_primary_band"]].copy()
        if "in_primary_band" in tradable.columns
        else tradable
    )
    rows.append(_print_briers("t24_primary", primary, "nbm_prob", "market_mid_carryforward"))
    if "season" in primary.columns:
        for season, group in primary.groupby("season"):
            rows.append(
                _print_briers(
                    f"t24_primary_{season}",
                    group,
                    "nbm_prob",
                    "market_mid_carryforward",
                )
            )
    return rows


def run(config: dict[str, Any], out_dir: Path, t12_dir: Path) -> int:
    storage = config.get("storage") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    books_path = out_dir / "k2_horizon_books.csv"
    trad_path = out_dir / "k2_t24_tradable.csv"
    if not books_path.exists():
        print("missing k2_horizon_books.csv — run analysis.k2_diagnostics first")
        return 1
    if not t12_dir.exists():
        print(f"missing T-12 decoded dir {t12_dir}")
        return 1

    summary_rows: list[dict[str, Any]] = []
    if trad_path.exists():
        print("=== T-24h primary-band paired Brier (disk, bootstrap 2000) ===")
        summary_rows.extend(t24_bootstrap_from_tradable(pd.read_csv(trad_path)))

    climate_dates = sample_climate_dates(config)
    all_markets = load_markets(raw_dir)
    climate_set = set(climate_dates)
    markets = [
        market
        for market in all_markets
        if ticker_climate_date(str(market.get("ticker") or "")) in climate_set
    ]
    books = pd.read_csv(books_path)
    t12_books = books[(books["horizon_h"] == 12) & books["in_primary_band"]].copy()

    rows: list[dict[str, Any]] = []
    n_t12 = 0
    for climate_date in climate_dates:
        ladder12 = load_nbm_ladder(t12_dir, climate_date)
        if ladder12 is None:
            continue
        n_t12 += 1
        day_markets = markets_for_climate_date(markets, climate_date)
        probs12 = bracket_probabilities_for_markets(ladder12, day_markets)
        day = t12_books[t12_books["climate_date"].astype(str) == climate_date]
        for _, snap in day.iterrows():
            ticker = str(snap["ticker"])
            if ticker not in probs12:
                continue
            mid = snap["market_mid_carryforward"]
            nbm12 = probs12[ticker]
            edge12 = (float(mid) - nbm12) if pd.notna(mid) else None
            rows.append(
                {
                    "climate_date": climate_date,
                    "season": snap.get("season"),
                    "ticker": ticker,
                    "strike_role": snap.get("strike_role"),
                    "nbm_prob_t24": snap["nbm_prob"],
                    "nbm_prob_t12": nbm12,
                    "market_mid": mid,
                    "settled_yes": snap["settled_yes"],
                    "edge_t24": snap["edge_cf"],
                    "edge_t12": edge12,
                    "taker_sell_t24": snap.get("taker_sell_yes_edge"),
                    "spread": snap.get("spread_carryforward"),
                }
            )
    frame = pd.DataFrame(rows)
    out_path = out_dir / "k2_t12_vs_t24.csv"
    frame.to_csv(out_path, index=False)
    print(f"t12_ladders={n_t12} rows={len(frame)} wrote {out_path}")
    if frame.empty:
        pd.DataFrame(summary_rows).to_csv(out_dir / "k2_t12_summary.csv", index=False)
        return 1

    print("=== T-12h primary band: frozen T-24 NBM vs T-12 NBM vs market ===")
    summary_rows.append(_print_briers("t12band_nbm24_vs_mkt", frame, "nbm_prob_t24", "market_mid"))
    summary_rows.append(_print_briers("t12band_nbm12_vs_mkt", frame, "nbm_prob_t12", "market_mid"))
    usable = _finite_pair(frame, "nbm_prob_t24", "nbm_prob_t12")
    if not usable.empty:
        yes = usable["settled_yes"].astype(float).to_numpy()
        t24 = usable["nbm_prob_t24"].astype(float).to_numpy()
        t12 = usable["nbm_prob_t12"].astype(float).to_numpy()
        vintage = paired_brier_diff(yes, t24, t12)
        print(
            f"t12band T-24 vs T-12 NBM n={len(usable)} "
            f"brier24={vintage['brier_a']:.4f} brier12={vintage['brier_b']:.4f} "
            f"24-12={vintage['mean']:+.4f} "
            f"95%CI=[{vintage['ci_lo']:+.4f},{vintage['ci_hi']:+.4f}]"
        )
        summary_rows.append(
            {
                "slice": "t12band_nbm24_minus_nbm12",
                "n": int(len(usable)),
                "nbm_brier": vintage["brier_a"],
                "market_brier": vintage["brier_b"],
                "nbm_minus_market": vintage["mean"],
                "nbm_minus_market_ci_lo": vintage["ci_lo"],
                "nbm_minus_market_ci_hi": vintage["ci_hi"],
                "frac_nbm_better": vintage["frac_a_better"],
            }
        )
    if "season" in frame.columns:
        for season, group in frame.groupby("season"):
            summary_rows.append(
                _print_briers(f"t12band_{season}_nbm12_vs_mkt", group, "nbm_prob_t12", "market_mid")
            )
    print(
        f"median_edge_t24={frame['edge_t24'].median():.4f} "
        f"median_edge_t12={frame['edge_t12'].median():.4f} "
        f"median_|nbm12-nbm24|="
        f"{(frame['nbm_prob_t12'] - frame['nbm_prob_t24']).abs().median():.4f}"
    )
    pd.DataFrame(summary_rows).to_csv(out_dir / "k2_t12_summary.csv", index=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-12 NBM vs frozen T-24 vs market")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    parser.add_argument("--t12-dir", type=Path, default=Path("data/nbm/decoded_v441_t12"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(load_config(args.config), args.out_dir, args.t12_dir)


if __name__ == "__main__":
    raise SystemExit(main())
