"""Compare CLINYC time-of-high against ASOS daily max under LST vs LDT hypotheses.

Allowed DB: none. Reads asos_obs JSONL and clinyc.csv only.

Measurement output only — the conclusion sentence is written to data-sources.md
after human readout.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_time import asos_max_for_climate_day, cli_max_instant
from ingestion.config_loader import load_config
from analysis.spread_census import select_full_day_labels

logger = logging.getLogger(__name__)

MATCH_TOLERANCE_MIN = 60


def _sample_days(labels: pd.DataFrame, *, n: int) -> list[str]:
    if labels.empty:
        return []
    labels = labels.sort_values("climate_date")
    if len(labels) <= n:
        return [str(d) for d in labels["climate_date"]]
    positions = [int(round(i * (len(labels) - 1) / (n - 1))) for i in range(n)]
    return [str(labels.iloc[pos]["climate_date"]) for pos in positions]


def build_clockb_table(
    *,
    labels: pd.DataFrame,
    raw_dir: Path,
    sample_days: int,
) -> pd.DataFrame:
    observations = load_asos_observations_from_raw(raw_dir)
    from ingestion.climate_day import climate_date_of

    obs_by_day: dict[str, list] = {}
    for obs in observations:
        climate = climate_date_of(obs.valid_utc).isoformat()
        obs_by_day.setdefault(climate, []).append(obs)

    days = _sample_days(labels, n=sample_days)
    rows: list[dict[str, Any]] = []
    for climate_date in days:
        label_rows = labels[labels["climate_date"] == climate_date]
        if label_rows.empty:
            continue
        label = label_rows.iloc[-1]
        time_raw = str(label.get("time_of_high_raw") or "")
        day_obs = obs_by_day.get(climate_date, [])
        asos_max_f, asos_max_ts = asos_max_for_climate_day(day_obs, climate_date)
        row: dict[str, Any] = {
            "climate_date": climate_date,
            "cli_time_raw": time_raw,
            "asos_max_F": asos_max_f,
            "asos_max_utc": asos_max_ts.isoformat() if asos_max_ts else None,
        }
        for hypothesis in ("lst", "ldt"):
            cli_instant = cli_max_instant(climate_date, time_raw, hypothesis)
            if cli_instant is None or asos_max_ts is None:
                row[f"delta_min_{hypothesis}"] = None
                row[f"consistent_{hypothesis}"] = False
                continue
            delta_min = abs((asos_max_ts - cli_instant).total_seconds()) / 60.0
            row[f"delta_min_{hypothesis}"] = round(delta_min, 1)
            row[f"consistent_{hypothesis}"] = delta_min <= MATCH_TOLERANCE_MIN
        rows.append(row)
    return pd.DataFrame(rows)


def run(config: dict[str, Any]) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    sample_days = int((config.get("clockb_check") or {}).get("sample_days") or 15)
    table = build_clockb_table(labels=labels, raw_dir=raw_dir, sample_days=sample_days)
    print(f"Clock B check: tolerance={MATCH_TOLERANCE_MIN} minutes; measurement only")
    if table.empty:
        print("no labeled days with ASOS data in range")
        return 0
    print(table.to_string(index=False))
    for hypothesis in ("lst", "ldt"):
        col = f"consistent_{hypothesis}"
        count = int(table[col].sum()) if col in table.columns else 0
        print(f"hypothesis {hypothesis.upper()}: {count}/{len(table)} days within tolerance")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clock B ASOS vs CLINYC calibration table")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--sample-days", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    if args.sample_days is not None:
        config.setdefault("clockb_check", {})["sample_days"] = args.sample_days
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
