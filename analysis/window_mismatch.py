"""Measure how often CLI time-of-high falls outside the NBM max window.

Allowed DB: none. Reads clinyc.csv only.

When climate.cli_time_convention is unknown, prints both LST and LDT hypothesis
rates by season and refuses a single headline number.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import season_of, select_full_day_labels
from ingestion.climate_time import (
    cli_max_instant,
    load_cli_time_convention,
    nbm_max_window_utc,
    time_in_window,
)
from ingestion.config_loader import load_config

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)


def _mismatch_rows(labels: pd.DataFrame, convention: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in labels.itertuples(index=False):
        climate_date = str(row.climate_date)
        time_raw = str(getattr(row, "time_of_high_raw", "") or "")
        instant = cli_max_instant(climate_date, time_raw, convention)  # type: ignore[arg-type]
        if instant is None:
            continue
        start, end = nbm_max_window_utc(climate_date)
        outside = not time_in_window(instant, start, end)
        rows.append(
            {
                "climate_date": climate_date,
                "season": season_of(climate_date),
                "convention": convention,
                "outside_window": outside,
            }
        )
    return pd.DataFrame(rows)


def summarize_by_season(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    grouped = frame.groupby(["season", "convention"], as_index=False)["outside_window"].mean()
    grouped = grouped.rename(columns={"outside_window": "mismatch_rate"})
    return grouped


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    convention = load_cli_time_convention(config)
    out_dir.mkdir(parents=True, exist_ok=True)

    if labels.empty:
        print("no full-day labels available")
        return 0

    if convention == "unknown":
        print("REFUSED: convention=unknown — printing both hypotheses, no headline rate")
        lst = _mismatch_rows(labels, "lst")
        ldt = _mismatch_rows(labels, "ldt")
        summary = summarize_by_season(pd.concat([lst, ldt], ignore_index=True))
        print(summary.to_string(index=False))
        csv_path = out_dir / "window_mismatch.csv"
        summary.to_csv(csv_path, index=False)
        _write_figure(summary, out_dir / "window_mismatch.png")
        print(f"wrote {csv_path}")
        return 0

    frame = _mismatch_rows(labels, convention)
    summary = summarize_by_season(frame)
    overall = float(frame["outside_window"].mean()) if len(frame) else 0.0
    print(f"convention={convention} overall mismatch rate: {overall:.4%}")
    print(summary.to_string(index=False))
    csv_path = out_dir / "window_mismatch.csv"
    summary.to_csv(csv_path, index=False)
    _write_figure(summary, out_dir / "window_mismatch.png")
    print(f"wrote {csv_path}")
    return 0


def _write_figure(summary: pd.DataFrame, path: Path) -> None:
    if summary.empty:
        return
    fig, ax = plt.subplots()
    for convention in sorted(summary["convention"].unique()):
        subset = summary[summary["convention"] == convention]
        ax.plot(subset["season"], subset["mismatch_rate"], marker="o", label=convention)
    ax.set_ylabel("mismatch rate")
    ax.set_xlabel("season")
    ax.set_title("CLI time-of-high outside NBM 12Z-06Z window")
    ax.legend()
    fig.savefig(path)
    plt.close(fig)
    print(f"wrote {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Window vs climate-day mismatch rate")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    default_out = (config.get("window_mismatch") or {}).get("out_dir") or "analysis/out"
    out_dir = args.out_dir or Path(default_out)
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
