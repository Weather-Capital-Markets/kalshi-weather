"""Prospective first-availability observer for NBM qmd .idx on AWS and NOMADS.

Polls not-yet-published cycles until the first HTTP 200 and records wall-clock
first availability vs Last-Modified header lag. Distinguishes mirror upload lag
(measured by nbm_latency_check) from operational first availability.

Allowed DB: none. Network: HTTP HEAD on AWS + NOMADS qmd .idx only.

Do not re-run nbm_archive until this settles the vintage rule (Session 6b-fix).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pandas as pd

from ingestion.config_loader import load_config
from ingestion.nbm_archive import (
    qmd_idx_url,
    snapshot_utc_for_climate_date,
    stratified_sample_climate_dates,
)
from ingestion.nbm_idx import (
    candidate_max_cycles_for_snapshot,
    forecast_hour_for_climate_max_window,
    publication_utc,
    vintage_select_cycle,
)
from analysis.nbm_latency_check import (
    METHOD_DESCRIPTION as LATENCY_METHOD_DESCRIPTION,
    mirror_lag_minutes,
    parse_last_modified_header,
    percentile,
)

logger = logging.getLogger(__name__)

METHOD_DESCRIPTION = (
    "HTTP HEAD on qmd .idx; first_availability_delta_min = "
    "(first_http_200_wall_clock_UTC - nominal_cycle_UTC). "
    "last_modified_delta_min uses Last-Modified on that first success. "
    "Gap between the two diagnoses metadata artifact vs real lag."
)

NOMADS_BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
SourceName = Literal["aws", "nomads"]

# Cycle hours to cover; forecast hour per hour for qmd idx polling.
CYCLE_PROBE_PLAN: tuple[tuple[int, int], ...] = (
    (0, 30),  # 00Z f030 — K2 climate-max vintage leg
    (6, 6),  # off-hour qmd (hourly ladder)
    (12, 30),  # 12Z f030
    (18, 6),
)

STABLE_BRACKET_CUTOFF = date(2022, 12, 11)


def nomads_idx_url(cycle_dt: datetime, forecast_hour: int) -> str:
    prefix = f"blend.{cycle_dt:%Y%m%d}/{cycle_dt:%H}"
    return (
        f"{NOMADS_BASE_URL}/{prefix}/qmd/"
        f"blend.t{cycle_dt:%H}z.qmd.f{forecast_hour:03d}.co.grib2.idx"
    )


def idx_url_for_source(
    source: SourceName,
    base_url: str,
    cycle_dt: datetime,
    forecast_hour: int,
) -> str:
    if source == "aws":
        return qmd_idx_url(base_url, cycle_dt, forecast_hour)
    return nomads_idx_url(cycle_dt, forecast_hour)


def availability_delta_minutes(nominal: datetime, first_seen: datetime) -> float:
    return (first_seen - nominal).total_seconds() / 60.0


def last_modified_gap_minutes(
    first_availability_delta_min: float,
    last_modified_delta_min: float | None,
) -> float | None:
    if last_modified_delta_min is None:
        return None
    return last_modified_delta_min - first_availability_delta_min


def cycle_nominal_utc(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def enumerate_upcoming_cycles(
    *,
    from_time: datetime,
    cycle_plan: tuple[tuple[int, int], ...] = CYCLE_PROBE_PLAN,
    lookback_days: int = 1,
    lookahead_days: int = 4,
) -> list[tuple[datetime, int]]:
    """Return (nominal_cycle, forecast_hour) sorted, including recent past cycles."""
    start_day = (from_time - timedelta(days=lookback_days)).date()
    end_day = (from_time + timedelta(days=lookahead_days)).date()
    items: list[tuple[datetime, int]] = []
    cursor = start_day
    while cursor <= end_day:
        for hour, forecast_hour in cycle_plan:
            nominal = cycle_nominal_utc(cursor, hour)
            items.append((nominal, forecast_hour))
        cursor += timedelta(days=1)
    deduped = sorted(set(items), key=lambda item: item[0])
    return deduped


def select_watch_cycles(
    *,
    from_time: datetime,
    min_cycles: int,
    completed_nominals: set[str],
    cycle_plan: tuple[tuple[int, int], ...] = CYCLE_PROBE_PLAN,
    max_watch_age_hours: float = 12.0,
) -> list[tuple[datetime, int]]:
    """Pick cycles to watch: incomplete, within prospective window."""
    lookback_days = max(1, int(max_watch_age_hours / 24) + 1)
    candidates = enumerate_upcoming_cycles(
        from_time=from_time,
        cycle_plan=cycle_plan,
        lookback_days=lookback_days,
        lookahead_days=1,
    )
    age_cutoff = from_time - timedelta(hours=max_watch_age_hours)
    incomplete = [
        (nominal, fh)
        for nominal, fh in candidates
        if nominal.isoformat() not in completed_nominals and nominal >= age_cutoff
    ]
    # Prefer cycles at or just after nominal time, but include soon-upcoming.
    active = [item for item in incomplete if item[0] <= from_time + timedelta(hours=6)]
    if len(active) < min_cycles:
        active = incomplete[: max(min_cycles, len(incomplete))]
    # Ensure each cycle hour in plan appears at least once when possible.
    by_hour: dict[int, tuple[datetime, int]] = {}
    for nominal, fh in active:
        hour = nominal.hour
        if hour not in by_hour or nominal > by_hour[hour][0]:
            by_hour[hour] = (nominal, fh)
    merged = sorted(by_hour.values(), key=lambda item: item[0])
    if len(merged) >= min_cycles:
        return merged[:min_cycles]
    # Pad with additional incomplete cycles.
    for nominal, fh in active:
        if (nominal, fh) not in merged:
            merged.append((nominal, fh))
        if len(merged) >= min_cycles:
            break
    return merged[:min_cycles]


@dataclass
class WatchRow:
    source: SourceName
    nominal_cycle_utc: datetime
    forecast_hour: int
    idx_url: str
    watch_started_utc: datetime | None = None
    first_http_200_utc: datetime | None = None
    first_availability_delta_min: float | None = None
    last_modified_utc: datetime | None = None
    last_modified_delta_min: float | None = None
    lm_vs_first_availability_gap_min: float | None = None
    http_status_at_first_success: int | None = None
    status: str = "pending"
    method: str = METHOD_DESCRIPTION

    @property
    def key(self) -> str:
        return (
            f"{self.source}|{self.nominal_cycle_utc.isoformat()}|f{self.forecast_hour:03d}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "nominal_cycle_utc": self.nominal_cycle_utc.isoformat(),
            "cycle_hour": self.nominal_cycle_utc.hour,
            "forecast_hour": self.forecast_hour,
            "idx_url": self.idx_url,
            "watch_started_utc": (
                self.watch_started_utc.isoformat() if self.watch_started_utc else ""
            ),
            "first_http_200_utc": (
                self.first_http_200_utc.isoformat() if self.first_http_200_utc else ""
            ),
            "first_availability_delta_min": self.first_availability_delta_min,
            "last_modified_utc": (
                self.last_modified_utc.isoformat() if self.last_modified_utc else ""
            ),
            "last_modified_delta_min": self.last_modified_delta_min,
            "lm_vs_first_availability_gap_min": self.lm_vs_first_availability_gap_min,
            "http_status_at_first_success": self.http_status_at_first_success,
            "status": self.status,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> WatchRow:
        nominal_raw = _optional_str(row.get("nominal_cycle_utc")) or ""
        nominal = datetime.fromisoformat(nominal_raw)
        watch_started = _optional_str(row.get("watch_started_utc"))
        first_200 = _optional_str(row.get("first_http_200_utc"))
        lm = _optional_str(row.get("last_modified_utc"))
        return WatchRow(
            source=str(row.get("source") or "aws"),
            nominal_cycle_utc=nominal,
            forecast_hour=int(row.get("forecast_hour") or 0),
            idx_url=str(row.get("idx_url") or ""),
            watch_started_utc=(
                datetime.fromisoformat(watch_started) if watch_started else None
            ),
            first_http_200_utc=(
                datetime.fromisoformat(first_200) if first_200 else None
            ),
            first_availability_delta_min=_optional_float(
                row.get("first_availability_delta_min")
            ),
            last_modified_utc=datetime.fromisoformat(lm) if lm else None,
            last_modified_delta_min=_optional_float(row.get("last_modified_delta_min")),
            lm_vs_first_availability_gap_min=_optional_float(
                row.get("lm_vs_first_availability_gap_min")
            ),
            http_status_at_first_success=_optional_int(
                row.get("http_status_at_first_success")
            ),
            status=str(row.get("status") or "pending"),
            method=str(row.get("method") or METHOD_DESCRIPTION),
        )


def _optional_str(value: Any) -> str | None:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    return int(value)


class NbmAvailabilityWatcher:
    def __init__(self, config: dict[str, Any]) -> None:
        nbm = config.get("nbm_archive") or {}
        watch = config.get("nbm_availability_watch") or {}
        self.aws_base_url = str(
            nbm.get("base_url") or "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
        )
        self.poll_interval_sec = int(watch.get("poll_interval_sec") or 300)
        self.min_cycles = int(watch.get("min_cycles") or 6)
        self.max_watch_age_hours = float(watch.get("max_watch_age_hours") or 12.0)
        self.sources: list[SourceName] = list(watch.get("sources") or ["aws", "nomads"])
        self.horizon_h = int(nbm.get("snapshot_horizon_h") or 24)
        self.assumed_latency_min = int(
            watch.get("assumed_latency_min") or nbm.get("publication_latency_min") or 60
        )
        self.client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "kalshi-weather-nbm-watch/1.0"},
        )

    def close(self) -> None:
        self.client.close()

    def head_idx(self, url: str) -> tuple[int, datetime | None]:
        response = self.client.head(url)
        if response.status_code == 403 and "nomads" in url:
            response = self.client.get(url, headers={"Range": "bytes=0-0"})
        last_modified = parse_last_modified_header(response.headers.get("Last-Modified"))
        status = response.status_code
        if status == 206:
            status = 200
        return status, last_modified

    def probe_row(self, row: WatchRow, now: datetime) -> WatchRow:
        if row.status in ("complete", "pre_existing"):
            return row
        if now < row.nominal_cycle_utc:
            row.status = "pending"
            return row
        if row.watch_started_utc is None:
            row.watch_started_utc = now
        status_code, last_modified = self.head_idx(row.idx_url)
        if status_code == 200:
            if row.status != "waiting":
                # First observation is already 200 — we did not witness publication.
                row.status = "pre_existing"
                row.http_status_at_first_success = status_code
                if last_modified is not None:
                    row.last_modified_utc = last_modified
                    row.last_modified_delta_min = mirror_lag_minutes(
                        row.nominal_cycle_utc,
                        last_modified,
                    )
                return row
            row.first_http_200_utc = now
            row.first_availability_delta_min = availability_delta_minutes(
                row.nominal_cycle_utc,
                now,
            )
            row.http_status_at_first_success = status_code
            if last_modified is not None:
                row.last_modified_utc = last_modified
                row.last_modified_delta_min = mirror_lag_minutes(
                    row.nominal_cycle_utc,
                    last_modified,
                )
                row.lm_vs_first_availability_gap_min = last_modified_gap_minutes(
                    row.first_availability_delta_min,
                    row.last_modified_delta_min,
                )
            row.status = "complete"
        else:
            row.status = "waiting"
        return row

    def build_initial_rows(
        self,
        *,
        now: datetime,
        existing: dict[str, WatchRow],
    ) -> list[WatchRow]:
        completed = {
            key
            for key, row in existing.items()
            if row.status == "complete"
        }
        completed_nominals = {
            row.nominal_cycle_utc.isoformat()
            for row in existing.values()
            if row.status == "complete"
        }
        cycles = select_watch_cycles(
            from_time=now,
            min_cycles=self.min_cycles,
            completed_nominals=completed_nominals,
            max_watch_age_hours=self.max_watch_age_hours,
        )
        rows: list[WatchRow] = []
        for nominal, forecast_hour in cycles:
            for source in self.sources:
                key_base = f"{source}|{nominal.isoformat()}|f{forecast_hour:03d}"
                if key_base in completed:
                    rows.append(existing[key_base])
                    continue
                if key_base in existing:
                    rows.append(existing[key_base])
                    continue
                url = idx_url_for_source(
                    source,
                    self.aws_base_url,
                    nominal,
                    forecast_hour,
                )
                rows.append(
                    WatchRow(
                        source=source,
                        nominal_cycle_utc=nominal,
                        forecast_hour=forecast_hour,
                        idx_url=url,
                    )
                )
        return rows

    def tick(self, rows: list[WatchRow], now: datetime) -> list[WatchRow]:
        return [self.probe_row(row, now) for row in rows]

    def run_blocking(
        self,
        *,
        out_dir: Path,
        max_wait_hours: float = 12.0,
    ) -> int:
        """Poll until min_cycles complete per source or deadline."""
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "nbm_availability_watch.csv"
        existing = load_watch_rows(csv_path)
        deadline = datetime.now(timezone.utc) + timedelta(hours=max_wait_hours)
        while datetime.now(timezone.utc) < deadline:
            now = datetime.now(timezone.utc)
            rows = self.build_initial_rows(now=now, existing=existing)
            rows = self.tick(rows, now)
            for row in rows:
                existing[row.key] = row
            save_watch_rows(csv_path, list(existing.values()))
            complete_by_source = {
                source: sum(
                    1
                    for row in existing.values()
                    if row.source == source and row.status == "complete"
                )
                for source in self.sources
            }
            if all(
                complete_by_source.get(source, 0) >= self.min_cycles
                for source in self.sources
            ):
                break
            if not any(row.status == "waiting" for row in rows):
                pending = any(row.status == "pending" for row in rows)
                if not pending:
                    break
            logger.info(
                "complete_by_source=%s waiting=%s",
                complete_by_source,
                sum(1 for row in rows if row.status == "waiting"),
            )
            time.sleep(self.poll_interval_sec)
        print_report(
            list(existing.values()),
            config_assumed_latency_min=self.assumed_latency_min,
            horizon_h=self.horizon_h,
        )
        return 0

    def run_tick(self, out_dir: Path) -> int:
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "nbm_availability_watch.csv"
        existing = load_watch_rows(csv_path)
        now = datetime.now(timezone.utc)
        rows = self.build_initial_rows(now=now, existing=existing)
        rows = self.tick(rows, now)
        for row in rows:
            existing[row.key] = row
        save_watch_rows(csv_path, list(existing.values()))
        complete_by_source = {
            source: sum(
                1
                for row in existing.values()
                if row.source == source and row.status == "complete"
            )
            for source in self.sources
        }
        logger.info("tick complete_by_source=%s wrote %s", complete_by_source, csv_path)
        if all(
            complete_by_source.get(source, 0) >= self.min_cycles
            for source in self.sources
        ):
            print_report(
                list(existing.values()),
                config_assumed_latency_min=self.assumed_latency_min,
                horizon_h=self.horizon_h,
            )
        return 0


def load_watch_rows(csv_path: Path) -> dict[str, WatchRow]:
    if not csv_path.exists():
        return {}
    frame = pd.read_csv(csv_path)
    rows: dict[str, WatchRow] = {}
    for _, series in frame.iterrows():
        row = WatchRow.from_dict(series.to_dict())
        rows[row.key] = row
    return rows


def save_watch_rows(csv_path: Path, rows: list[WatchRow]) -> None:
    frame = pd.DataFrame([row.to_dict() for row in sorted(rows, key=lambda r: r.key)])
    frame.to_csv(csv_path, index=False)


def summarize_deltas(
    rows: list[WatchRow],
    *,
    source: SourceName,
    delta_field: str,
) -> dict[str, float | int]:
    complete = [
        row
        for row in rows
        if row.source == source and row.status == "complete"
    ]
    values = [
        float(row.to_dict()[delta_field])
        for row in complete
        if row.to_dict().get(delta_field) is not None
    ]
    if not values:
        return {"n": 0}
    by_hour: dict[int, list[float]] = {}
    for row in complete:
        val = row.to_dict().get(delta_field)
        if val is None:
            continue
        by_hour.setdefault(row.nominal_cycle_utc.hour, []).append(float(val))
    hour_stats = {
        f"hour_{hour:02d}z_median": percentile(vals, 0.5)
        for hour, vals in sorted(by_hour.items())
    }
    return {
        "n": len(values),
        "median_min": percentile(values, 0.5),
        "p90_min": percentile(values, 0.9),
        "max_min": max(values),
        **hour_stats,
    }


def vintage_availability_verdict(
    *,
    aws_rows: list[WatchRow],
    assumed_latency_min: int,
    horizon_h: int,
    example_climate_date: date | None = None,
) -> list[str]:
    """Which cycle would have been available at T-24h for 00Z/f030 vintage."""
    lines: list[str] = []
    climate = example_climate_date or date.today() + timedelta(days=1)
    snapshot = snapshot_utc_for_climate_date(climate, horizon_h)
    candidates = candidate_max_cycles_for_snapshot(snapshot)
    covering = [
        cycle
        for cycle in candidates
        if forecast_hour_for_climate_max_window(cycle, climate) is not None
    ]
    vintage = vintage_select_cycle(
        snapshot,
        covering,
        latency_min=assumed_latency_min,
    )
    if vintage is None:
        lines.append(
            f"example_climate_date={climate.isoformat()} snapshot={snapshot.isoformat()} "
            "no vintage under assumed_latency_min rule"
        )
        return lines

    forecast_hour = forecast_hour_for_climate_max_window(vintage, climate)
    lines.append(
        f"example_climate_date={climate.isoformat()} snapshot={snapshot.isoformat()} "
        f"current_vintage_rule={vintage.isoformat()} f{forecast_hour:03d}"
    )

    # Map measured first-availability for matching 00Z f030 AWS rows.
    lag_by_nominal: dict[str, float] = {}
    for row in aws_rows:
        if row.status != "complete" or row.first_availability_delta_min is None:
            continue
        if row.nominal_cycle_utc.hour == 0 and row.forecast_hour == 30:
            lag_by_nominal[row.nominal_cycle_utc.isoformat()] = (
                row.first_availability_delta_min
            )

    if vintage.isoformat() in lag_by_nominal:
        measured = lag_by_nominal[vintage.isoformat()]
        pub_at = vintage + timedelta(minutes=measured)
        safe = pub_at < snapshot
        lines.append(
            f"measured_first_availability_for_vintage delta_min={measured:.1f} "
            f"available_at={pub_at.isoformat()} safe_at_snapshot={safe}"
        )

    # Find latest cycle that would have been available using measured median 00Z lag.
    zeroz_lags = [
        row.first_availability_delta_min
        for row in aws_rows
        if row.status == "complete"
        and row.source == "aws"
        and row.nominal_cycle_utc.hour == 0
        and row.forecast_hour == 30
        and row.first_availability_delta_min is not None
    ]
    if zeroz_lags:
        median_lag = percentile(zeroz_lags, 0.5)
        available_cycles = [
            cycle
            for cycle in covering
            if cycle + timedelta(minutes=median_lag) < snapshot
        ]
        if available_cycles:
            best = max(available_cycles)
            fh = forecast_hour_for_climate_max_window(best, climate)
            lead_h = None
            if fh is not None:
                lead_h = fh
            lines.append(
                f"using_median_00z_f030_first_avail_lag_min={median_lag:.1f} "
                f"latest_available_cycle={best.isoformat()} f{fh:03d} "
                f"implied_forecast_hour={lead_h}"
            )
        else:
            lines.append(
                f"using_median_00z_f030_first_avail_lag_min={median_lag:.1f} "
                "no 00Z/12Z cycle available at snapshot"
            )

    return lines


def print_report(
    rows: list[WatchRow],
    *,
    config_assumed_latency_min: int,
    horizon_h: int,
) -> None:
    aws_rows = [row for row in rows if row.source == "aws"]
    nomads_rows = [row for row in rows if row.source == "nomads"]

    print(f"method={METHOD_DESCRIPTION}")
    print(f"latency_check_method_note={LATENCY_METHOD_DESCRIPTION}")
    print("\n=== first-availability delta (AWS) ===")
    fa_aws = summarize_deltas(aws_rows, source="aws", delta_field="first_availability_delta_min")
    for key, value in fa_aws.items():
        if isinstance(value, float):
            print(f"{key}={value:.1f}")
        else:
            print(f"{key}={value}")

    print("\n=== Last-Modified delta (AWS, same cycles) ===")
    lm_aws = summarize_deltas(aws_rows, source="aws", delta_field="last_modified_delta_min")
    for key, value in lm_aws.items():
        if isinstance(value, float):
            print(f"{key}={value:.1f}")
        else:
            print(f"{key}={value}")

    pre_existing_lm = [
        row.last_modified_delta_min
        for row in aws_rows
        if row.status == "pre_existing" and row.last_modified_delta_min is not None
    ]
    if pre_existing_lm:
        print("\n=== Last-Modified delta (AWS pre_existing — metadata only, not first-avail) ===")
        print(f"n={len(pre_existing_lm)} median_min={percentile(pre_existing_lm, 0.5):.1f}")

    print("\n=== first-availability delta (NOMADS) ===")
    fa_nomads = summarize_deltas(
        nomads_rows,
        source="nomads",
        delta_field="first_availability_delta_min",
    )
    for key, value in fa_nomads.items():
        if isinstance(value, float):
            print(f"{key}={value:.1f}")
        else:
            print(f"{key}={value}")

    gaps = [
        row.lm_vs_first_availability_gap_min
        for row in aws_rows
        if row.status == "complete" and row.lm_vs_first_availability_gap_min is not None
    ]
    if gaps:
        print("\n=== AWS Last-Modified minus first-availability gap (min) ===")
        print(f"median_gap_min={percentile(gaps, 0.5):.1f} p90_gap_min={percentile(gaps, 0.9):.1f}")

    print("\n=== verdict ===")
    fa_median = fa_aws.get("median_min")
    lm_median = lm_aws.get("median_min")
    nomads_median = fa_nomads.get("median_min")
    if fa_median is not None and lm_median is not None:
        gap = lm_median - fa_median
        if gap > 120:
            print(
                "Last-Modified substantially exceeds first HTTP 200 — "
                "435-min hard stop likely metadata artifact (bulk re-upload / lifecycle)."
            )
        elif fa_median > 120:
            print(
                "First availability itself is multi-hour — mirror lag is real; "
                "T-24h 00Z/f030 vintage rule is not operable on AWS timing."
            )
        else:
            print(
                "First availability aligns with NOAA ~30-50 min publication band; "
                "435-min Last-Modified lag is not operational availability."
            )
    if nomads_median is not None and fa_median is not None:
        if nomads_median < fa_median - 30:
            print(
                "NOMADS leads AWS on first availability — historical vintage rule "
                "should follow NOMADS-equivalent timing, not mirror Last-Modified."
            )

    print("\n=== vintage / T-24h snapshot (00Z f030) ===")
    for line in vintage_availability_verdict(
        aws_rows=aws_rows,
        assumed_latency_min=config_assumed_latency_min,
        horizon_h=horizon_h,
    ):
        print(line)

    print("\n=== K2 sample scope (stable 6-bracket regime) ===")
    for line in audit_sample_scope():
        print(line)


def audit_sample_scope() -> list[str]:
    config = load_config()
    nbm = config.get("nbm_archive") or {}
    start = date.fromisoformat(str(nbm.get("start_date") or "2021-08-05"))
    end = date.fromisoformat(str(nbm.get("end_date") or "2026-05-03"))
    split = date.fromisoformat(str(nbm.get("sub_era_split") or "2024-05-15"))
    target_n = int(nbm.get("sample_size") or 300)
    seed = int(nbm.get("sample_seed") or 42)
    sample = stratified_sample_climate_dates(
        start,
        end,
        target_n=target_n,
        sub_era_split=split,
        seed=seed,
    )
    before = [d for d in sample if d < STABLE_BRACKET_CUTOFF]
    return [
        f"nbm_sample_n={len(sample)} before_{STABLE_BRACKET_CUTOFF.isoformat()}={len(before)}",
        f"eligible_after_cutoff={len(sample) - len(before)} (need re-sample or drop {len(before)} days for K2)",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBM qmd idx first-availability watch")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=["tick", "run", "report"],
        default="tick",
        help="tick=single poll (systemd timer); run=blocking wait; report=CSV only",
    )
    parser.add_argument(
        "--max-wait-hours",
        type=float,
        default=12.0,
        help="Blocking run deadline",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    watch_cfg = config.get("nbm_availability_watch") or {}
    default_out = watch_cfg.get("out_dir") or "analysis/out"
    out_dir = args.out_dir or Path(default_out)
    watcher = NbmAvailabilityWatcher(config)
    try:
        if args.mode == "report":
            csv_path = out_dir / "nbm_availability_watch.csv"
            rows = list(load_watch_rows(csv_path).values())
            print_report(
                rows,
                config_assumed_latency_min=watcher.assumed_latency_min,
                horizon_h=watcher.horizon_h,
            )
            return 0
        if args.mode == "run":
            return watcher.run_blocking(out_dir=out_dir, max_wait_hours=args.max_wait_hours)
        return watcher.run_tick(out_dir)
    finally:
        watcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
