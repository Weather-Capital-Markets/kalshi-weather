"""Point-in-time NBM qmd archive extraction (retrospective leg).

Allowed DB: nbm_archive.backfill_db (falls back to storage.backfill_db).
Never open storage.heartbeat_db. Kalshi/CLINYC/ASOS resume stays on storage.backfill_db.

Uses AWS .idx sidecars and HTTP byte-range requests — never downloads whole grib2
files (283 MB each). See knowledge/data-sources.md §2–§3, §5.1.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from ingestion.climate_day import climate_day_end
from ingestion.config_loader import load_config
from ingestion.heartbeat import connect, init_schema
from ingestion.nbm_decode import decode_message_at_gridpoint, nearest_gridpoint_from_grib
from ingestion.nbm_idx import (
    QMD_WINDOW_FORECAST_HOURS,
    ByteRange,
    VintageSelection,
    assert_vintage_available,
    available_max_cycles,
    byte_ranges_for_selected_lines,
    empirical_vintage_for_climate_date,
    era_band_for_date,
    era_level_count_for_date,
    max_product_for_cycle_hour,
    nbm_version_for_date,
    parse_idx_text,
    publication_utc,
    select_max_window_percentile_lines,
    target_max_window_end_utc,
    target_max_window_start_utc,
)
from ingestion.nbm_ladder import apply_isotonic_to_ladder, dedupe_ladder_by_percentile
from ingestion.state import (
    init_backfill_schema,
    init_state_schema,
    nbm_day_complete,
    set_nbm_day_complete,
)
from ingestion.validate_units import assert_non_empty_frame
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)

GRIDPOINT_POLICY = (
    "nearest_grid_cell: haversine distance to configured station lat/lon on the "
    "decoded CONUS qmd lattice (closes data-sources.md O8)."
)

DEFAULT_PERCENTILE_LEVELS: tuple[int, ...] = (10, 20, 30, 40, 50, 60, 70, 80, 90)
SEASON_MONTHS: dict[str, set[int]] = {
    "DJF": {12, 1, 2},
    "MAM": {3, 4, 5},
    "JJA": {6, 7, 8},
    "SON": {9, 10, 11},
}
V4_SUB_ERAS: tuple[str, ...] = ("early", "late")


def season_of_date(climate_date: date) -> str:
    month = climate_date.month
    for season, months in SEASON_MONTHS.items():
        if month in months:
            return season
    raise ValueError(f"invalid month for climate date: {climate_date.isoformat()}")


def v4_sub_era(climate_date: date, *, split: date) -> str:
    return "early" if climate_date < split else "late"


def stratified_sample_climate_dates(
    start: date,
    end: date,
    *,
    target_n: int,
    sub_era_split: date,
    seed: int,
) -> list[date]:
    """Sample climate dates across season × v4 sub-era strata (deterministic)."""
    pool: dict[tuple[str, str], list[date]] = defaultdict(list)
    for climate_date in climate_date_range(start, end):
        if climate_date >= date(2026, 5, 4):
            continue
        pool[(season_of_date(climate_date), v4_sub_era(climate_date, split=sub_era_split))].append(
            climate_date
        )
    if not pool:
        return []
    strata = sorted(pool)
    base = target_n // len(strata)
    remainder = target_n % len(strata)
    rng = random.Random(seed)
    sampled: list[date] = []
    for index, key in enumerate(strata):
        quota = base + (1 if index < remainder else 0)
        candidates = pool[key]
        if quota >= len(candidates):
            sampled.extend(candidates)
        else:
            sampled.extend(rng.sample(candidates, quota))
    return sorted(sampled)


def summarize_sample_strata(
    dates: list[date],
    *,
    sub_era_split: date,
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for climate_date in dates:
        counts[(season_of_date(climate_date), v4_sub_era(climate_date, split=sub_era_split))] += 1
    return dict(sorted(counts.items()))


def climate_date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def snapshot_utc_for_climate_date(climate_date: date, horizon_h: int) -> datetime:
    end = climate_day_end(climate_date)
    return end - timedelta(hours=horizon_h)


def blend_prefix(cycle_dt: datetime) -> str:
    return f"blend.{cycle_dt:%Y%m%d}/{cycle_dt:%H}"


def qmd_grib_url(base_url: str, cycle_dt: datetime, forecast_hour: int) -> str:
    prefix = blend_prefix(cycle_dt)
    return (
        f"{base_url.rstrip('/')}/{prefix}/qmd/"
        f"blend.t{cycle_dt:%H}z.qmd.f{forecast_hour:03d}.co.grib2"
    )


def qmd_idx_url(base_url: str, cycle_dt: datetime, forecast_hour: int) -> str:
    return qmd_grib_url(base_url, cycle_dt, forecast_hour) + ".idx"


def select_forecast_hours(cycle_hour: int) -> list[int]:
    return [fh for fh in QMD_WINDOW_FORECAST_HOURS if max_product_for_cycle_hour(cycle_hour, fh)]


class NbmArchiveClient:
    def __init__(self, base_url: str, timeout_sec: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout_sec, follow_redirects=True)
        self.bytes_transferred = 0

    def close(self) -> None:
        self.client.close()

    def fetch_text(self, url: str) -> tuple[int, str]:
        response = self.client.get(url)
        self.bytes_transferred += len(response.content)
        return response.status_code, response.text

    def fetch_range(self, url: str, byte_range: ByteRange) -> tuple[int, bytes]:
        response = self.client.get(url, headers={"Range": byte_range.header_value()})
        content = response.content
        self.bytes_transferred += len(content)
        return response.status_code, content


class NbmArchiveBackfill:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        nbm = config.get("nbm_archive") or {}
        self.base_url = str(nbm.get("base_url") or "https://noaa-nbm-grib2-pds.s3.amazonaws.com")
        self.latency_min = int(nbm.get("publication_latency_min") or 441)
        self.latency_max_min = int(
            nbm.get("publication_latency_max_min") or max(self.latency_min, 453)
        )
        self.station_lat = float(nbm.get("station_lat") or 40.779)
        self.station_lon = float(nbm.get("station_lon") or -73.969)
        self.start_date = date.fromisoformat(str(nbm.get("start_date") or "2022-12-11"))
        self.end_date = date.fromisoformat(str(nbm.get("end_date") or "2026-05-03"))
        self.horizon_h = int(nbm.get("snapshot_horizon_h") or 24)
        self.decoded_dir = Path(str(nbm.get("decoded_dir") or "data/nbm/decoded_v441"))
        raw_levels = nbm.get("percentile_levels") or list(DEFAULT_PERCENTILE_LEVELS)
        self.percentile_levels = tuple(int(level) for level in raw_levels)
        self.percentile_level_set = set(self.percentile_levels)
        self.sample_size = int(nbm.get("sample_size") or 300)
        self.sample_seed = int(nbm.get("sample_seed") or 43)
        self.sub_era_split = date.fromisoformat(
            str(nbm.get("sub_era_split") or "2024-05-15"),
        )
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.backfill_db_path = Path(str(nbm.get("backfill_db") or storage["backfill_db"]))
        self.conn = connect(self.backfill_db_path)
        init_schema(self.conn)
        init_state_schema(self.conn)
        init_backfill_schema(self.conn)
        self.http = NbmArchiveClient(self.base_url)
        self._grid_row_col: tuple[int, int] | None = None
        self._grid_meta: tuple[float, float, float] | None = None

    def sampled_climate_dates(self) -> list[date]:
        return stratified_sample_climate_dates(
            self.start_date,
            self.end_date,
            target_n=self.sample_size,
            sub_era_split=self.sub_era_split,
            seed=self.sample_seed,
        )

    def _expected_ladder_levels(self) -> int:
        return len(self.percentile_levels)

    def estimate_day_grib_bytes_from_idx(self, climate_date: date) -> tuple[int, int]:
        """Return (grib_range_bytes, idx_bytes) for one climate date; idx fetch only."""
        selection = self.empirical_vintage_for_climate_date(climate_date)
        if selection is None:
            return 0, 0
        vintage = selection.cycle
        forecast_hour = selection.forecast_hour
        idx_url = qmd_idx_url(self.base_url, vintage, forecast_hour)
        status, idx_text = self.http.fetch_text(idx_url)
        idx_bytes = len(idx_text.encode("utf-8")) if idx_text else 0
        if status != 200:
            return 0, idx_bytes
        all_lines = parse_idx_text(idx_text)
        pct_lines = select_max_window_percentile_lines(
            all_lines,
            forecast_hour=forecast_hour,
            percentile_levels=self.percentile_level_set,
        )
        ranges = byte_ranges_for_selected_lines(all_lines, pct_lines)
        return sum(item.size for item in ranges), idx_bytes

    def _default_probe_date(self) -> date:
        dates = self.sampled_climate_dates()
        if dates:
            return dates[0]
        return date(2022, 12, 11)

    def vintage_calibrate(self, climate_date: date) -> int:
        snapshot = snapshot_utc_for_climate_date(climate_date, self.horizon_h)
        print(f"=== NBM vintage calibration climate_date={climate_date.isoformat()} ===")
        print(f"snapshot_utc={snapshot.isoformat()}")
        print(f"window_start_utc={target_max_window_start_utc(climate_date).isoformat()}")
        print(f"window_end_utc={target_max_window_end_utc(climate_date).isoformat()}")
        print(
            f"publication_latency_min={self.latency_min} "
            f"publication_latency_max_min={self.latency_max_min}"
        )
        print("\n=== candidate 00Z/12Z cycles (pub at max latency strictly before snapshot) ===")
        for cycle in available_max_cycles(snapshot, latency_max_min=self.latency_max_min):
            pub_max = publication_utc(cycle, self.latency_max_min)
            pub_p90 = publication_utc(cycle, self.latency_min)
            print(
                f"  {cycle.isoformat()} pub_p90={pub_p90.isoformat()} "
                f"pub_max={pub_max.isoformat()} margin_p90_min="
                f"{(snapshot - pub_p90).total_seconds() / 60.0:.1f}"
            )
        selection = self.empirical_vintage_for_climate_date(climate_date)
        if selection is None:
            print("\nFINDING: no empirical vintage with idx-confirmed climate max window")
            return 1
        print("\n=== selected vintage ===")
        print(f"vintage_cycle_utc={selection.cycle.isoformat()}")
        print(f"forecast_hour=f{selection.forecast_hour:03d}")
        print(f"forecast_lead_h={selection.forecast_lead_h:.1f}")
        print(f"publication_utc_p90={selection.publication_utc_p90.isoformat()}")
        print(f"publication_utc_max={selection.publication_utc_max.isoformat()}")
        print(f"snapshot_margin_min_p90={selection.snapshot_margin_min_p90:.1f}")
        print(f"snapshot_margin_min_max={selection.snapshot_margin_min_max:.1f}")
        print(f"\n=== matched idx lines ({len(selection.matched_lines)}) ===")
        for line in selection.matched_lines:
            print(line.raw_line)
        return self.probe(climate_date)

    def close(self) -> None:
        self.writer.close()
        self.http.close()
        self.conn.close()

    def _ensure_grid(self, sample_bytes: bytes) -> tuple[int, int]:
        if self._grid_row_col is not None:
            return self._grid_row_col
        found = nearest_gridpoint_from_grib(
            sample_bytes,
            target_lat=self.station_lat,
            target_lon=self.station_lon,
        )
        if found is None:
            raise RuntimeError("failed to read lat/lon from sample grib message")
        row, col, grid_lat, grid_lon, distance_km = found
        self._grid_row_col = (row, col)
        self._grid_meta = (grid_lat, grid_lon, distance_km)
        return row, col

    def fetch_percentile_ladder(
        self,
        cycle_dt: datetime,
        forecast_hour: int,
    ) -> tuple[list[dict[str, Any]], list[ByteRange], int]:
        idx_url = qmd_idx_url(self.base_url, cycle_dt, forecast_hour)
        grib_url = qmd_grib_url(self.base_url, cycle_dt, forecast_hour)
        status, idx_text = self.http.fetch_text(idx_url)
        if status != 200:
            logger.warning("idx fetch failed %s status=%s", idx_url, status)
            return [], [], 0
        all_lines = parse_idx_text(idx_text)
        pct_lines = select_max_window_percentile_lines(
            all_lines,
            forecast_hour=forecast_hour,
            percentile_levels=self.percentile_level_set,
        )
        if not pct_lines:
            return [], [], 0
        ranges = byte_ranges_for_selected_lines(all_lines, pct_lines)
        ladder: list[dict[str, Any]] = []
        bytes_before = self.http.bytes_transferred
        row_col: tuple[int, int] | None = None
        for byte_range in ranges:
            grib_status, grib_bytes = self.http.fetch_range(grib_url, byte_range)
            if grib_status not in (200, 206) or not grib_bytes:
                continue
            if row_col is None:
                row_col = self._ensure_grid(grib_bytes)
            row, col = row_col
            value_f = decode_message_at_gridpoint(grib_bytes, row=row, col=col)
            if value_f is None:
                logger.warning(
                    "decode failed at gridpoint row=%s col=%s offset=%s",
                    row,
                    col,
                    byte_range.start,
                )
                continue
            ladder.append(
                {
                    "percentile_level": byte_range.idx_line.percentile_level,
                    "value_f": value_f,
                    "byte_offset": byte_range.start,
                    "byte_size": byte_range.size,
                    "idx_line": byte_range.idx_line.raw_line,
                }
            )
        bytes_used = self.http.bytes_transferred - bytes_before
        return ladder, ranges, bytes_used

    def _fetch_idx_text(self, cycle_dt: datetime, forecast_hour: int) -> str | None:
        idx_url = qmd_idx_url(self.base_url, cycle_dt, forecast_hour)
        status, idx_text = self.http.fetch_text(idx_url)
        if status != 200 or not idx_text:
            return None
        return idx_text

    def empirical_vintage_for_climate_date(
        self,
        climate_date: date,
    ) -> VintageSelection | None:
        snapshot = snapshot_utc_for_climate_date(climate_date, self.horizon_h)
        return empirical_vintage_for_climate_date(
            climate_date,
            snapshot,
            fetch_idx=self._fetch_idx_text,
            latency_p90_min=self.latency_min,
            latency_max_min=self.latency_max_min,
            percentile_levels=self.percentile_level_set,
        )

    def vintage_cycle_for_climate_date(self, climate_date: date) -> datetime | None:
        selection = self.empirical_vintage_for_climate_date(climate_date)
        return selection.cycle if selection is not None else None

    def process_climate_date(self, climate_date: date, *, persist: bool = True) -> dict[str, Any]:
        snapshot = snapshot_utc_for_climate_date(climate_date, self.horizon_h)
        selection = self.empirical_vintage_for_climate_date(climate_date)
        result: dict[str, Any] = {
            "climate_date": climate_date.isoformat(),
            "snapshot_utc": snapshot.isoformat(),
            "publication_latency_min": self.latency_min,
            "publication_latency_max_min": self.latency_max_min,
            "nbm_version": nbm_version_for_date(climate_date),
            "era_band": era_band_for_date(climate_date),
            "level_count": era_level_count_for_date(climate_date),
            "gridpoint_policy": GRIDPOINT_POLICY,
            "station_lat": self.station_lat,
            "station_lon": self.station_lon,
        }
        if selection is None:
            result["status"] = "no_vintage_cycle"
            return result
        vintage = selection.cycle
        forecast_hour = selection.forecast_hour
        result["vintage_cycle_utc"] = vintage.isoformat()
        result["publication_utc"] = selection.publication_utc_p90.isoformat()
        result["publication_utc_max"] = selection.publication_utc_max.isoformat()
        result["snapshot_margin_min_p90"] = selection.snapshot_margin_min_p90
        result["snapshot_margin_min_max"] = selection.snapshot_margin_min_max
        result["forecast_lead_h"] = selection.forecast_lead_h
        result["matched_idx_lines"] = [line.raw_line for line in selection.matched_lines]
        assert_vintage_available(vintage, snapshot, latency_p90_min=self.latency_min)
        ladder, ranges, bytes_used = self.fetch_percentile_ladder(vintage, forecast_hour)
        ladder, dedupe_dropped = dedupe_ladder_by_percentile(ladder)
        if dedupe_dropped:
            result["dedupe_dropped_n"] = dedupe_dropped
        ladder, isotonic_changed = apply_isotonic_to_ladder(ladder)
        if isotonic_changed:
            result["isotonic_adjusted"] = True
        result["forecast_hour"] = forecast_hour
        result["percentile_ladder"] = ladder
        result["idx_ranges"] = [r.header_value() for r in ranges]
        result["bytes_transferred"] = bytes_used
        if self._grid_meta is not None:
            grid_lat, grid_lon, distance_km = self._grid_meta
            result["grid_lat"] = grid_lat
            result["grid_lon"] = grid_lon
            result["grid_distance_km"] = distance_km
        expected_levels = self._expected_ladder_levels()
        if ladder and len(ladder) < expected_levels:
            result["status"] = "partial_ladder"
            result["ladder_count"] = len(ladder)
            result["expected_levels"] = expected_levels
            return result

        if ladder and persist:
            grib_url = qmd_grib_url(self.base_url, vintage, forecast_hour)
            idx_url = qmd_idx_url(self.base_url, vintage, forecast_hour)
            for entry in ladder:
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=idx_url,
                    category="nbm_qmd",
                    key=(
                        f"{climate_date.isoformat()}_{vintage:%Y%m%d%H}_f{forecast_hour:03d}_"
                        f"p{entry['percentile_level']}"
                    ),
                    http_status=206,
                    latency_ms=0,
                    payload={
                        "climate_date": climate_date.isoformat(),
                        "vintage_cycle_utc": vintage.isoformat(),
                        "forecast_hour": forecast_hour,
                        "percentile_level": entry["percentile_level"],
                        "value_f": entry["value_f"],
                        "byte_offset": entry["byte_offset"],
                        "byte_size": entry["byte_size"],
                        "grib_url": grib_url,
                    },
                )
            self.decoded_dir.mkdir(parents=True, exist_ok=True)
            frame = pd.DataFrame(ladder)
            for col in (
                "climate_date",
                "snapshot_utc",
                "vintage_cycle_utc",
                "publication_utc",
                "publication_utc_max",
                "publication_latency_min",
                "publication_latency_max_min",
                "snapshot_margin_min_p90",
                "snapshot_margin_min_max",
                "forecast_hour",
                "forecast_lead_h",
                "nbm_version",
                "era_band",
                "level_count",
                "grid_lat",
                "grid_lon",
                "grid_distance_km",
            ):
                frame[col] = result.get(col)
            out_path = self.decoded_dir / f"{climate_date.isoformat()}.parquet"
            assert_non_empty_frame(
                frame,
                what=f"NBM parquet ladder for {climate_date.isoformat()}",
            )
            frame.to_parquet(out_path, index=False)
            result["decoded_path"] = str(out_path)
            result["status"] = "ok"
        elif ladder:
            result["status"] = "ok"
        else:
            result["status"] = "empty_ladder"
        return result

    def probe(self, probe_date: date | None = None) -> int:
        when = probe_date or self._default_probe_date()
        print(f"=== NBM archive probe climate_date={when.isoformat()} ===")
        print(
            f"publication_latency_min={self.latency_min} "
            f"publication_latency_max_min={self.latency_max_min}"
        )
        print(f"gridpoint_policy={GRIDPOINT_POLICY}")
        snapshot = snapshot_utc_for_climate_date(when, self.horizon_h)
        print(f"snapshot_utc={snapshot.isoformat()}")
        result = self.process_climate_date(when, persist=False)
        print(f"vintage_cycle_utc={result.get('vintage_cycle_utc')}")
        print(f"publication_utc={result.get('publication_utc')}")
        print(f"publication_utc_max={result.get('publication_utc_max')}")
        print(f"snapshot_margin_min_p90={result.get('snapshot_margin_min_p90')}")
        print(f"snapshot_margin_min_max={result.get('snapshot_margin_min_max')}")
        forecast_hour = result.get("forecast_hour")
        if forecast_hour is not None:
            print(f"forecast_hour=f{int(forecast_hour):03d}")
        else:
            print("forecast_hour=None")
        print(f"forecast_lead_h={result.get('forecast_lead_h')}")
        print(f"nbm_version={result.get('nbm_version')} level_count={result.get('level_count')}")
        matched_lines = result.get("matched_idx_lines") or []
        print(f"\n=== matched idx lines ({len(matched_lines)}) ===")
        for line in matched_lines:
            print(line)
        ladder = result.get("percentile_ladder") or []
        print("\n=== byte ranges ===")
        for header in result.get("idx_ranges") or []:
            print(header)
        print("\n=== decoded percentile ladder (°F) at KNYC gridpoint ===")
        for entry in ladder:
            raw = entry.get("value_f_raw")
            if raw is not None and abs(float(raw) - float(entry["value_f"])) > 1e-9:
                print(
                    f"P{entry['percentile_level']:>3}%  {entry['value_f']:.2f} F "
                    f"(raw={float(raw):.2f})"
                )
            else:
                print(f"P{entry['percentile_level']:>3}%  {entry['value_f']:.2f} F")
        if result.get("grid_lat") is not None:
            print(
                f"\ngrid_lat={result['grid_lat']:.4f} grid_lon={result['grid_lon']:.4f} "
                f"distance_km={result['grid_distance_km']:.3f}"
            )
        print(f"\ntotal_bytes_transferred={self.http.bytes_transferred}")
        return 0 if ladder else 1

    def dry_run(self) -> int:
        dates = self.sampled_climate_dates()
        strata = summarize_sample_strata(dates, sub_era_split=self.sub_era_split)
        print("scope=sampled v4 retrospective leg (no bulk grib downloads)")
        print(
            f"eligible_span={self.start_date.isoformat()}..{self.end_date.isoformat()} "
            f"sub_era_split={self.sub_era_split.isoformat()}"
        )
        print(f"percentile_levels={list(self.percentile_levels)} (n={len(self.percentile_levels)})")
        print(f"sample_size={len(dates)} target={self.sample_size} seed={self.sample_seed}")
        print("sample_strata (season, sub_era) -> count:")
        for key, count in strata.items():
            print(f"  {key[0]}/{key[1]} -> {count}")

        reps: dict[tuple[str, str], date] = {}
        for climate_date in dates:
            key = (
                season_of_date(climate_date),
                v4_sub_era(climate_date, split=self.sub_era_split),
            )
            reps.setdefault(key, climate_date)

        grib_per_day: list[int] = []
        idx_per_day: list[int] = []
        print("\nidx-calibrated bytes per stratum representative day:")
        for key in sorted(reps):
            rep = reps[key]
            grib_bytes, idx_bytes = self.estimate_day_grib_bytes_from_idx(rep)
            grib_per_day.append(grib_bytes)
            idx_per_day.append(idx_bytes)
            print(
                f"  {key[0]}/{key[1]} rep={rep.isoformat()} "
                f"grib_range_bytes={grib_bytes:,} idx_bytes={idx_bytes:,}"
            )

        if not grib_per_day:
            print("estimated_total_bytes=0 (no idx calibration succeeded)")
            return 1

        avg_grib = sum(grib_per_day) / len(grib_per_day)
        avg_idx = sum(idx_per_day) / len(idx_per_day)
        total_grib = int(avg_grib * len(dates))
        total_idx = int(avg_idx * len(dates))
        total_bytes = total_grib + total_idx
        print(f"\nbytes_per_day~={int(avg_grib + avg_idx):,} "
              f"(avg {len(self.percentile_levels)} grib ranges + idx)")
        print(f"estimated_grib_bytes~={total_grib:,}")
        print(f"estimated_idx_bytes~={total_idx:,}")
        print(f"estimated_total_bytes~={total_bytes:,} ({total_bytes / 1e9:.2f} GB)")
        print("dry-run: idx fetched for calibration only; no grib byte-range downloads")
        return 0

    def backfill(self) -> int:
        for climate_date in self.sampled_climate_dates():
            key = climate_date.isoformat()
            if nbm_day_complete(self.conn, key):
                continue
            result = self.process_climate_date(climate_date)
            if result.get("status") == "ok":
                set_nbm_day_complete(self.conn, key, utc_now_iso())
                logger.info("nbm complete %s bytes=%s", key, result.get("bytes_transferred"))
            else:
                logger.warning("nbm skip %s status=%s", key, result.get("status"))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBM qmd archive backfill (byte-range only)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-date", type=str, default=None)
    parser.add_argument("--vintage-calibrate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    app = NbmArchiveBackfill(config)
    try:
        probe_date = date.fromisoformat(args.probe_date) if args.probe_date else None
        if args.vintage_calibrate:
            when = probe_date or app._default_probe_date()
            return app.vintage_calibrate(when)
        if args.probe:
            return app.probe(probe_date)
        if args.dry_run:
            return app.dry_run()
        return app.backfill()
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
