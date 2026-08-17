"""Compare CLINYC time-of-high against ASOS daily max under LST vs LDT hypotheses.

Allowed DB: none. Reads asos_obs JSONL and clinyc.csv only.

Measurement output only — distributions and summary stats; no conclusion sentence.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import select_full_day_labels
from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import climate_date_of, climate_dst_status
from ingestion.climate_time import asos_max_for_climate_day, cli_max_instant
from ingestion.config_loader import load_config

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

HIST_BIN_MIN = 5
HIST_MAX_MIN = 120


def _delta_minutes(asos_ts, cli_instant) -> float | None:
    if asos_ts is None or cli_instant is None:
        return None
    return abs((asos_ts - cli_instant).total_seconds()) / 60.0


def build_clockb_table(
    *,
    labels: pd.DataFrame,
    raw_dir: Path,
) -> pd.DataFrame:
    observations = load_asos_observations_from_raw(raw_dir)
    obs_by_day: dict[str, list] = {}
    for obs in observations:
        climate = climate_date_of(obs.valid_utc).isoformat()
        obs_by_day.setdefault(climate, []).append(obs)

    rows: list[dict[str, Any]] = []
    if labels.empty:
        return pd.DataFrame(rows)

    for row in labels.sort_values("climate_date").itertuples(index=False):
        climate_date = str(row.climate_date)
        time_raw = str(getattr(row, "time_of_high_raw", "") or "")
        if not time_raw.strip():
            continue
        day_obs = obs_by_day.get(climate_date, [])
        asos_max_f, asos_max_ts = asos_max_for_climate_day(day_obs, climate_date)
        if asos_max_ts is None:
            continue
        record: dict[str, Any] = {
            "climate_date": climate_date,
            "dst_status": climate_dst_status(climate_date),
            "cli_time_raw": time_raw,
            "asos_max_F": asos_max_f,
            "asos_max_utc": asos_max_ts.isoformat(),
        }
        for hypothesis in ("lst", "ldt"):
            cli_instant = cli_max_instant(climate_date, time_raw, hypothesis)
            delta = _delta_minutes(asos_max_ts, cli_instant)
            record[f"delta_{hypothesis}"] = round(delta, 1) if delta is not None else None
        if record.get("delta_lst") is None and record.get("delta_ldt") is None:
            continue
        rows.append(record)
    return pd.DataFrame(rows)


def _iqr_minutes(series: pd.Series) -> float:
    clean = series.dropna()
    if clean.empty:
        return float("nan")
    return float(clean.quantile(0.75) - clean.quantile(0.25))


def _plot_delta_histogram(series: pd.Series, path: Path, title: str) -> None:
    clean = series.dropna()
    if clean.empty:
        return
    bins = list(range(0, HIST_MAX_MIN + HIST_BIN_MIN, HIST_BIN_MIN))
    plt.figure(figsize=(8, 4))
    plt.hist(clean, bins=bins, edgecolor="black", linewidth=0.5)
    plt.xlabel("delta (minutes)")
    plt.ylabel("climate days")
    plt.title(title)
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()


def _print_dst_block(
    frame: pd.DataFrame,
    *,
    label: str,
    out_dir: Path,
    file_suffix: str,
) -> None:
    n = len(frame)
    print(f"\n=== {label} (n={n}) ===")
    if frame.empty:
        print("no days")
        return
    for hypothesis in ("lst", "ldt"):
        col = f"delta_{hypothesis}"
        series = frame[col]
        print(
            f"delta_{hypothesis}: median={series.median():.1f} min, "
            f"IQR={_iqr_minutes(series):.1f} min"
        )
    lst_path = out_dir / f"clockb_delta_lst_{file_suffix}.png"
    ldt_path = out_dir / f"clockb_delta_ldt_{file_suffix}.png"
    _plot_delta_histogram(frame["delta_lst"], lst_path, f"delta_lst — {label}")
    _plot_delta_histogram(frame["delta_ldt"], ldt_path, f"delta_ldt — {label}")
    print(f"histogram delta_lst: {lst_path}")
    print(f"histogram delta_ldt: {ldt_path}")


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    table = build_clockb_table(labels=labels, raw_dir=raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Clock B check: measurement only (no conclusion)")
    print(
        "Hourly ASOS quantizes observed max time; under the true hypothesis delta "
        "should center near zero with roughly ±30 min spread, while the false "
        "hypothesis centers near 60."
    )

    if table.empty:
        print("no labeled days with ASOS coverage in range")
        return 0

    csv_path = out_dir / "clockb_days.csv"
    table.to_csv(csv_path, index=False)
    print(f"per-day table: {csv_path} ({len(table)} rows)")

    edt = table[table["dst_status"] == "edt"]
    est = table[table["dst_status"] == "est"]
    transition = table[table["dst_status"] == "transition"]

    _print_dst_block(
        edt,
        label="EDT days (identifying)",
        out_dir=out_dir,
        file_suffix="edt",
    )
    _print_dst_block(
        est,
        label="EST days (non-identifying; not pooled with EDT)",
        out_dir=out_dir,
        file_suffix="est",
    )
    if not transition.empty:
        print(f"\n=== DST transition days (excluded from EDT pool, n={len(transition)}) ===")
        print("listed in per-day table; not used for EDT headline stats")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clock B ASOS vs CLINYC calibration")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    clockb = config.get("clockb_check") or {}
    out_dir = args.out_dir or Path(str(clockb.get("out_dir") or "analysis/out"))
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
