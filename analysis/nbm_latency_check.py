"""Measure NBM qmd idx mirror availability lag vs nominal cycle time.

Allowed DB: none. Network: HTTP HEAD on AWS qmd .idx sidecars only.

Verifies the publication_latency_min assumption used by nbm_archive vintage
selection. Hard-stops when measured p90 mirror lag exceeds the assumed latency.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from ingestion.config_loader import load_config
from ingestion.nbm_archive import qmd_idx_url, snapshot_utc_for_climate_date
from ingestion.nbm_idx import (
    QMD_WINDOW_FORECAST_HOURS,
    candidate_max_cycles_for_snapshot,
    forecast_hour_for_climate_max_window,
    max_product_for_cycle_hour,
    publication_utc,
    vintage_select_cycle,
)

logger = logging.getLogger(__name__)

METHOD_DESCRIPTION = (
    "HTTP HEAD on AWS qmd .idx sidecars; mirror_lag_min = "
    "(Last-Modified UTC - nominal_cycle_UTC). Last-Modified is mirror upload time, "
    "not NOAA internal publication time."
)


def parse_last_modified_header(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def mirror_lag_minutes(nominal: datetime, last_modified: datetime) -> float:
    return (last_modified - nominal).total_seconds() / 60.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def evaluate_hard_stop(lags: list[float], *, assumed_min: int) -> tuple[bool, float, float, float]:
    if not lags:
        return True, float("nan"), float("nan"), float("nan")
    median = percentile(lags, 0.5)
    p90 = percentile(lags, 0.9)
    maximum = max(lags)
    return p90 > assumed_min, median, p90, maximum


def forecast_hour_for_cycle(cycle: datetime) -> int | None:
    for forecast_hour in QMD_WINDOW_FORECAST_HOURS:
        if max_product_for_cycle_hour(cycle.hour, forecast_hour):
            return forecast_hour
    return None


def panel_a_cycles(*, end_day: date, days: int = 12) -> list[datetime]:
    cycles: list[datetime] = []
    for offset in range(days):
        day = end_day - timedelta(days=offset)
        for hour in (0, 12):
            cycles.append(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc))
    return sorted(cycles)


def panel_b_climate_dates(*, end_day: date, count: int = 20) -> list[date]:
    dates: list[date] = []
    cursor = end_day
    while len(dates) < count and cursor >= date(2021, 8, 5):
        if cursor < date(2026, 5, 4):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(dates)


class NbmLatencyChecker:
    def __init__(self, config: dict[str, Any]) -> None:
        nbm = config.get("nbm_archive") or {}
        check = config.get("nbm_latency_check") or {}
        self.base_url = str(nbm.get("base_url") or "https://noaa-nbm-grib2-pds.s3.amazonaws.com")
        self.assumed_latency_min = int(
            check.get("assumed_latency_min") or nbm.get("publication_latency_min") or 60
        )
        self.horizon_h = int(nbm.get("snapshot_horizon_h") or 24)
        self.min_cycles = int(check.get("min_cycles") or 20)
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def head_idx(self, cycle: datetime, forecast_hour: int) -> dict[str, Any]:
        url = qmd_idx_url(self.base_url, cycle, forecast_hour)
        response = self.client.head(url)
        last_modified = parse_last_modified_header(response.headers.get("Last-Modified"))
        lag = mirror_lag_minutes(cycle, last_modified) if last_modified is not None else None
        return {
            "panel": "A",
            "nominal_cycle_utc": cycle.isoformat(),
            "forecast_hour": forecast_hour,
            "idx_url": url,
            "http_status": response.status_code,
            "last_modified_utc": last_modified.isoformat() if last_modified else "",
            "mirror_lag_min": lag,
            "publication_utc_assumed": publication_utc(
                cycle,
                self.assumed_latency_min,
            ).isoformat(),
            "method": METHOD_DESCRIPTION,
        }

    def probe_panel_a(self, *, end_day: date | None = None) -> pd.DataFrame:
        end = end_day or date.today() - timedelta(days=1)
        rows: list[dict[str, Any]] = []
        for cycle in panel_a_cycles(end_day=end):
            forecast_hour = forecast_hour_for_cycle(cycle)
            if forecast_hour is None:
                continue
            rows.append(self.head_idx(cycle, forecast_hour))
        return pd.DataFrame(rows)

    def probe_panel_b(self, *, end_day: date | None = None) -> pd.DataFrame:
        end = end_day or min(date.today() - timedelta(days=1), date(2026, 5, 3))
        rows: list[dict[str, Any]] = []
        for climate_date in panel_b_climate_dates(end_day=end):
            snapshot = snapshot_utc_for_climate_date(climate_date, self.horizon_h)
            candidates = candidate_max_cycles_for_snapshot(snapshot)
            covering = [
                cycle
                for cycle in candidates
                if forecast_hour_for_climate_max_window(cycle, climate_date) is not None
            ]
            vintage = vintage_select_cycle(
                snapshot,
                covering,
                latency_min=self.assumed_latency_min,
            )
            if vintage is None:
                rows.append(
                    {
                        "panel": "B",
                        "climate_date": climate_date.isoformat(),
                        "snapshot_utc": snapshot.isoformat(),
                        "vintage_cycle_utc": "",
                        "forecast_hour": None,
                        "vintage_safe_at_snapshot": False,
                        "mirror_lag_min": None,
                        "method": METHOD_DESCRIPTION,
                    }
                )
                continue
            forecast_hour = forecast_hour_for_climate_max_window(vintage, climate_date)
            assert forecast_hour is not None
            head = self.head_idx(vintage, forecast_hour)
            last_modified_raw = head.get("last_modified_utc") or ""
            last_modified = (
                datetime.fromisoformat(last_modified_raw) if last_modified_raw else None
            )
            vintage_safe = (
                last_modified is not None
                and last_modified < snapshot
                and head["http_status"] == 200
            )
            rows.append(
                {
                    "panel": "B",
                    "climate_date": climate_date.isoformat(),
                    "snapshot_utc": snapshot.isoformat(),
                    "vintage_cycle_utc": vintage.isoformat(),
                    "forecast_hour": forecast_hour,
                    "vintage_safe_at_snapshot": vintage_safe,
                    "mirror_lag_min": head.get("mirror_lag_min"),
                    "idx_url": head.get("idx_url"),
                    "http_status": head.get("http_status"),
                    "last_modified_utc": head.get("last_modified_utc"),
                    "publication_utc_assumed": head.get("publication_utc_assumed"),
                    "method": METHOD_DESCRIPTION,
                }
            )
        return pd.DataFrame(rows)

    def run(self, out_dir: Path) -> int:
        panel_a = self.probe_panel_a()
        panel_b = self.probe_panel_b()
        if len(panel_a) < self.min_cycles:
            logger.warning("panel A returned %s cycles (< min %s)", len(panel_a), self.min_cycles)

        lags = [
            float(value)
            for value in panel_a["mirror_lag_min"].dropna().tolist()
            if value is not None
        ]
        hard_stop, median, p90, maximum = evaluate_hard_stop(
            lags,
            assumed_min=self.assumed_latency_min,
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "nbm_latency_check.csv"
        combined = pd.concat([panel_a, panel_b], ignore_index=True, sort=False)
        combined.to_csv(csv_path, index=False)

        print(f"method={METHOD_DESCRIPTION}")
        print(f"assumed_latency_min={self.assumed_latency_min}")
        print(f"panel_A_cycles={len(panel_a)} panel_B_climate_dates={len(panel_b)}")
        print("\n=== mirror lag distribution (Panel A) ===")
        print(f"median_min={median:.1f} p90_min={p90:.1f} max_min={maximum:.1f}")

        if panel_b["vintage_safe_at_snapshot"].notna().any():
            safe_rate = float(panel_b["vintage_safe_at_snapshot"].mean())
            print(
                f"\nPanel B vintage_safe_at_snapshot rate={safe_rate:.1%} "
                "(Last-Modified before T-24h snapshot for selected vintage)"
            )

        print("\n=== HARD STOP evaluation ===")
        print(f"HARD_STOP = p90 > assumed_latency_min -> {hard_stop}")
        if hard_stop:
            print(
                "BLOCKING: measured p90 mirror lag exceeds assumed publication latency. "
                "The completed NBM backfill may have selected vintages under a latency "
                "assumption that understates mirror availability delay. Re-run nbm_archive "
                "with corrected latency / vintage rule before K2 (Session 6c)."
            )
            print(f"wrote {csv_path}")
            return 2

        print("PASS: p90 mirror lag is within assumed publication latency.")
        print(f"wrote {csv_path}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBM qmd idx mirror latency check")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    default_out = (config.get("nbm_latency_check") or {}).get("out_dir") or "analysis/out"
    out_dir = args.out_dir or Path(default_out)
    checker = NbmLatencyChecker(config)
    try:
        return checker.run(out_dir)
    finally:
        checker.close()


if __name__ == "__main__":
    raise SystemExit(main())
