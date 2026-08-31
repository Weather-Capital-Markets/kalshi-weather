"""Point-in-time GEFS archive extraction (Session 9).

Allowed DB: data/backfill_gefs.sqlite only. Never open heartbeat.sqlite or
NBM backfill DBs. Never write data/nbm/.

Byte-range grib GET only. Whole-file grib downloads are forbidden.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from analysis.nbm_availability_watch import (
    NbmAvailabilityWatcher,
    WatchRow,
    enumerate_upcoming_cycles,
    load_watch_rows,
    save_watch_rows,
)
from analysis.nbm_latency_check import parse_last_modified_header, percentile
from ingestion.config_loader import load_config
from ingestion.gefs_decode import decode_grid_cells, nearest_gridpoint_from_grib
from ingestion.gefs_idx import (
    MEMBERS,
    VintageAvailabilityError,
    byte_ranges_for_gefs_lines,
    forecast_hours_for_climate_day,
    gefs_grib_url,
    gefs_idx_url,
    parse_idx_text,
    select_tmax_2m_lines,
    select_tmp_2m_lines,
    select_vintage,
    snapshot_utc_for_climate_date,
)
from ingestion.heartbeat import connect, init_schema
from ingestion.state import (
    gefs_day_complete,
    init_backfill_schema,
    set_gefs_day_complete,
)
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)

GEFS_CYCLE_PLAN: tuple[tuple[int, int], ...] = ((0, 6), (6, 6), (12, 6), (18, 6))
BYTE_CAP_GB = 30.0
GRIDPOINT_POLICY = (
    "nearest_grid_cell: haversine to configured station lat/lon on the GEFS "
    "0.5 deg pgrb2a lattice. Instantaneous 2 m TMP at 3-hourly valid times; "
    "accumulating 0-N hour TMAX is recorded but not used for daily max."
)


class LatencyNotPinnedError(RuntimeError):
    """publication_latency_min is unset; vintage selection is refused."""


class GefsArchiveClient:
    def __init__(self, timeout_sec: float = 120.0) -> None:
        self.client = httpx.Client(timeout=timeout_sec, follow_redirects=True)
        self.bytes_transferred = 0
        self.max_retries = 4

    def close(self) -> None:
        self.client.close()

    def fetch_text(self, url: str) -> tuple[int, str]:
        if not url.endswith(".idx"):
            raise ValueError(f"full GET is only allowed for .idx: {url}")
        response = self._get(url)
        self.bytes_transferred += len(response.content)
        return response.status_code, response.text

    def fetch_range(self, url: str, header_value: str) -> tuple[int, bytes]:
        if url.endswith(".idx"):
            raise ValueError(f"grib range GET used on idx url: {url}")
        response = self._get(url, headers={"Range": header_value})
        self.bytes_transferred += len(response.content)
        return response.status_code, response.content

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self.client.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                time.sleep(2.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc


def gefs_select_watch_cycles(
    *,
    from_time: datetime,
    min_cycles: int,
    completed_nominals: set[str],
    max_watch_age_hours: float = 12.0,
) -> list[tuple[datetime, int]]:
    """Issued cycles still in the age window, then the next unpublished ones.

    NBM's select_watch_cycles keeps the latest 00/06/12/18Z in a lookahead
    window, which for GEFS is tomorrow's full set - all still pending, none
    polled. GEFS needs the last couple of issued cycles plus the next few.
    """
    candidates = enumerate_upcoming_cycles(
        from_time=from_time,
        cycle_plan=GEFS_CYCLE_PLAN,
        lookback_days=max(2, int(max_watch_age_hours / 24) + 1),
        lookahead_days=2,
    )
    age_cutoff = from_time - timedelta(hours=max_watch_age_hours)
    horizon = from_time + timedelta(hours=18)
    eligible = [
        (nominal, fh)
        for nominal, fh in candidates
        if age_cutoff <= nominal <= horizon and nominal.isoformat() not in completed_nominals
    ]
    past = [item for item in eligible if item[0] <= from_time]
    future = [item for item in eligible if item[0] > from_time]
    selected = past[-2:] + future
    if len(selected) < min_cycles:
        extra = [item for item in eligible if item not in selected]
        selected = selected + extra
    return selected[:min_cycles]


def prune_stale_watch_rows(
    rows: dict[str, WatchRow],
    *,
    now: datetime,
    horizon_hours: float = 18.0,
) -> dict[str, WatchRow]:
    """Drop pending rows whose cycle is beyond the watch horizon."""
    cutoff = now + timedelta(hours=horizon_hours)
    kept: dict[str, WatchRow] = {}
    for key, row in rows.items():
        if row.status == "pending" and row.nominal_cycle_utc > cutoff:
            continue
        kept[key] = row
    return kept


class GefsAvailabilityWatcher(NbmAvailabilityWatcher):
    """First-HTTP-200 observer for GEFS .idx on AWS. Reuses NBM poll machinery."""

    def __init__(self, config: dict[str, Any]) -> None:
        gefs = config.get("gefs_archive") or {}
        watch = config.get("gefs_availability_watch") or {}
        synthetic = {
            "nbm_archive": {
                "base_url": gefs.get("base_url") or "https://noaa-gefs-pds.s3.amazonaws.com",
                "snapshot_horizon_h": gefs.get("snapshot_horizon_h") or 24,
                "publication_latency_min": gefs.get("publication_latency_min") or 0,
            },
            "nbm_availability_watch": {
                "poll_interval_sec": watch.get("poll_interval_sec") or 300,
                "min_cycles": watch.get("min_cycles") or 4,
                "max_watch_age_hours": watch.get("max_watch_age_hours") or 12,
                "sources": watch.get("sources") or ["aws"],
            },
        }
        super().__init__(synthetic)
        self.gefs_base_url = str(gefs.get("base_url") or "https://noaa-gefs-pds.s3.amazonaws.com")
        self.sentinel_fh = int(gefs.get("sentinel_forecast_hour") or 6)
        self.product = str(gefs.get("product") or "pgrb2ap5")
        self.resolution = str(gefs.get("resolution") or "0p50")

    def head_idx(self, url: str) -> tuple[int, datetime | None]:
        """S3 often 403s HEAD; Range GET of the first byte is the NBM NOMADS pattern."""
        response = self.client.get(url, headers={"Range": "bytes=0-0"})
        last_modified = parse_last_modified_header(response.headers.get("Last-Modified"))
        status = response.status_code
        if status in {200, 206}:
            status = 200
        return status, last_modified

    def build_initial_rows(
        self,
        *,
        now: datetime,
        existing: dict[str, WatchRow],
    ) -> list[WatchRow]:
        completed_nominals = {
            row.nominal_cycle_utc.isoformat()
            for row in existing.values()
            if row.status == "complete"
        }
        cycles = gefs_select_watch_cycles(
            from_time=now,
            min_cycles=self.min_cycles,
            completed_nominals=completed_nominals,
            max_watch_age_hours=self.max_watch_age_hours,
        )
        rows: list[WatchRow] = []
        for nominal, _fh in cycles:
            forecast_hour = self.sentinel_fh
            key = f"aws|{nominal.isoformat()}|f{forecast_hour:03d}"
            if key in existing:
                rows.append(existing[key])
                continue
            url = gefs_idx_url(
                self.gefs_base_url,
                nominal,
                "gec00",
                forecast_hour,
                product=self.product,
                resolution=self.resolution,
            )
            rows.append(
                WatchRow(
                    source="aws",
                    nominal_cycle_utc=nominal,
                    forecast_hour=forecast_hour,
                    idx_url=url,
                )
            )
        return rows


def sample_climate_dates_from_nbm(decoded_dir: Path) -> list[date]:
    dates = [date.fromisoformat(path.stem) for path in sorted(decoded_dir.glob("*.parquet"))]
    return dates


def require_latency(latency_min: int | None) -> int:
    if latency_min is None:
        raise LatencyNotPinnedError(
            "publication_latency_min is unset. Run --latency-probe, pin p90, wait for OK."
        )
    return int(latency_min)


class GefsArchiveBackfill:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        gefs = config.get("gefs_archive") or {}
        self.base_url = str(gefs.get("base_url") or "https://noaa-gefs-pds.s3.amazonaws.com")
        raw_latency = gefs.get("publication_latency_min")
        self.latency_min = int(raw_latency) if raw_latency is not None else None
        raw_max = gefs.get("publication_latency_max_min")
        self.latency_max_min = int(raw_max) if raw_max is not None else self.latency_min
        self.station_lat = float(gefs.get("station_lat") or 40.779)
        self.station_lon = float(gefs.get("station_lon") or -73.969)
        self.horizon_h = int(gefs.get("snapshot_horizon_h") or 24)
        self.decoded_dir = Path(str(gefs.get("decoded_dir") or "data/gefs/decoded"))
        self.nbm_sample_dir = Path(str(gefs.get("nbm_sample_dir") or "data/nbm/decoded_v441"))
        self.product = str(gefs.get("product") or "pgrb2ap5")
        self.resolution = str(gefs.get("resolution") or "0p50")
        backfill_db = Path(str(gefs.get("backfill_db") or "data/backfill_gefs.sqlite"))
        self.conn = connect(backfill_db)
        init_schema(self.conn)
        init_backfill_schema(self.conn)
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.http = GefsArchiveClient()
        self._grid: tuple[int, int, float, float, float] | None = None

    def close(self) -> None:
        self.http.close()
        self.conn.close()

    def sampled_climate_dates(self) -> list[date]:
        return sample_climate_dates_from_nbm(self.nbm_sample_dir)

    def _idx_url(self, cycle: datetime, member: str, forecast_hour: int) -> str:
        return gefs_idx_url(
            self.base_url,
            cycle,
            member,
            forecast_hour,
            product=self.product,
            resolution=self.resolution,
        )

    def _grib_url(self, cycle: datetime, member: str, forecast_hour: int) -> str:
        return gefs_grib_url(
            self.base_url,
            cycle,
            member,
            forecast_hour,
            product=self.product,
            resolution=self.resolution,
        )

    def _ensure_grid(self, grib_bytes: bytes) -> tuple[int, int]:
        if self._grid is None:
            point = nearest_gridpoint_from_grib(
                grib_bytes,
                target_lat=self.station_lat,
                target_lon=self.station_lon,
            )
            if point is None:
                raise RuntimeError("failed to locate KNYC gridpoint on GEFS grib")
            self._grid = point
        return self._grid[0], self._grid[1]

    def tmp_range_from_idx(self, idx_text: str) -> tuple[list[Any], Any, list[Any]]:
        lines = parse_idx_text(idx_text)
        tmp = select_tmp_2m_lines(lines)
        tmax = select_tmax_2m_lines(lines)
        ranges = byte_ranges_for_gefs_lines(lines, tmp)
        return lines, ranges[0] if ranges else None, tmax

    def print_idx_inspect(self, cycle: datetime, climate_date: date, hours: list[int]) -> int:
        print(
            f"inspect_cycle_utc={cycle.isoformat()} climate_date={climate_date.isoformat()} "
            "(not a vintage unless latency is pinned)"
        )
        print(f"f_hours={hours}")
        if not hours:
            print("no 3-hourly valid times in the climate-day window")
            return 1
        idx_url = self._idx_url(cycle, "gec00", hours[0])
        status, idx_text = self.http.fetch_text(idx_url)
        print(f"idx_url={idx_url} http={status}")
        if status != 200:
            return 1
        lines = parse_idx_text(idx_text)
        tmp = select_tmp_2m_lines(lines)
        tmax = select_tmax_2m_lines(lines)
        print("\n=== matched idx (control, first climate-day f-hour) ===")
        for line in tmp:
            print(f"TMP  {line.raw_line}")
        for line in tmax:
            print(f"TMAX {line.raw_line} accumulating={line.is_accumulating_tmax}")
        print(
            "NOTE: climate-day max uses instantaneous TMP. Accumulating TMAX "
            "is recorded here and not mixed in."
        )
        return 0

    def process_climate_date(self, climate_date: date, *, persist: bool = True) -> dict[str, Any]:
        latency = require_latency(self.latency_min)
        latency_max = int(self.latency_max_min if self.latency_max_min is not None else latency)
        snapshot = snapshot_utc_for_climate_date(climate_date, self.horizon_h)
        vintage = select_vintage(
            climate_date,
            snapshot,
            latency_p90_min=latency,
            latency_max_min=latency_max,
        )
        members: list[dict[str, Any]] = []
        bytes_used = 0
        for member in MEMBERS:
            values_f: list[float] = []
            hours_used: list[int] = []
            for forecast_hour in vintage.forecast_hours:
                idx_url = self._idx_url(vintage.cycle, member, forecast_hour)
                status, idx_text = self.http.fetch_text(idx_url)
                if status != 200:
                    raise RuntimeError(f"idx HTTP {status} {idx_url}")
                _lines, tmp_range, _tmax = self.tmp_range_from_idx(idx_text)
                if tmp_range is None:
                    raise RuntimeError(f"no TMP 2 m line in {idx_url}")
                grib_url = self._grib_url(vintage.cycle, member, forecast_hour)
                grib_status, grib_bytes = self.http.fetch_range(grib_url, tmp_range.header_value())
                if grib_status not in {200, 206}:
                    raise RuntimeError(f"grib HTTP {grib_status} {grib_url}")
                bytes_used += len(grib_bytes)
                row, col = self._ensure_grid(grib_bytes)
                decoded = decode_grid_cells(grib_bytes, [("cell", row, col)])
                value = decoded.get("cell")
                if value is None:
                    raise RuntimeError(f"decode failed {member} f{forecast_hour:03d}")
                values_f.append(float(value))
                hours_used.append(forecast_hour)
                if persist:
                    self.writer.write(
                        ts_utc=utc_now_iso(),
                        endpoint=idx_url,
                        category="gefs_pgrb2a",
                        key=(
                            f"{climate_date.isoformat()}_{vintage.cycle:%Y%m%d%H}_"
                            f"{member}_f{forecast_hour:03d}"
                        ),
                        http_status=grib_status,
                        latency_ms=0,
                        payload={
                            "climate_date": climate_date.isoformat(),
                            "member": member,
                            "forecast_hour": forecast_hour,
                            "value_f": float(value),
                            "grib_url": grib_url,
                        },
                    )
            members.append(
                {
                    "member": member,
                    "tmax_f": max(values_f),
                    "n_hours": len(values_f),
                    "values_f": values_f,
                    "forecast_hours": hours_used,
                }
            )
        result: dict[str, Any] = {
            "status": "ok",
            "climate_date": climate_date.isoformat(),
            "snapshot_utc": snapshot.isoformat(),
            "vintage_cycle_utc": vintage.cycle.isoformat(),
            "publication_utc": vintage.publication_utc_p90.isoformat(),
            "publication_latency_min": latency,
            "publication_latency_max_min": latency_max,
            "snapshot_margin_min_p90": vintage.snapshot_margin_min_p90,
            "forecast_hours": list(vintage.forecast_hours),
            "n_members": len(members),
            "bytes_transferred": bytes_used,
            "temp_kind": "tmp",
            "members": members,
        }
        if self._grid is not None:
            _row, _col, grid_lat, grid_lon, distance_km = self._grid
            result["grid_lat"] = grid_lat
            result["grid_lon"] = grid_lon
            result["grid_distance_km"] = distance_km
        if persist:
            self.decoded_dir.mkdir(parents=True, exist_ok=True)
            frame = pd.DataFrame(
                [
                    {
                        "climate_date": climate_date.isoformat(),
                        "member": item["member"],
                        "tmax_f": item["tmax_f"],
                        "n_hours": item["n_hours"],
                        "snapshot_utc": result["snapshot_utc"],
                        "vintage_cycle_utc": result["vintage_cycle_utc"],
                        "publication_utc": result["publication_utc"],
                        "publication_latency_min": latency,
                        "snapshot_margin_min_p90": result["snapshot_margin_min_p90"],
                        "forecast_hours": ",".join(str(h) for h in item["forecast_hours"]),
                        "grid_lat": result.get("grid_lat"),
                        "grid_lon": result.get("grid_lon"),
                        "grid_distance_km": result.get("grid_distance_km"),
                        "temp_kind": "tmp",
                    }
                    for item in members
                ]
            )
            out_path = self.decoded_dir / f"{climate_date.isoformat()}.parquet"
            frame.to_parquet(out_path, index=False)
            result["decoded_path"] = str(out_path)
        return result

    def probe(self, probe_date: date | None = None) -> int:
        dates = self.sampled_climate_dates()
        when = probe_date or (dates[0] if dates else date(2022, 12, 15))
        print(f"=== GEFS archive probe climate_date={when.isoformat()} ===")
        print(f"publication_latency_min={self.latency_min}")
        print(f"gridpoint_policy={GRIDPOINT_POLICY}")
        snapshot = snapshot_utc_for_climate_date(when, self.horizon_h)
        print(f"snapshot_utc={snapshot.isoformat()}")
        if self.latency_min is None:
            cycle = datetime(when.year, when.month, when.day, 18, tzinfo=timezone.utc) - timedelta(
                days=1
            )
            hours = forecast_hours_for_climate_day(cycle, when)
            print("latency unset; idx inspect only -- decode waits on pinned p90")
            return self.print_idx_inspect(cycle, when, hours)
        result = self.process_climate_date(when, persist=False)
        print(f"vintage_cycle_utc={result['vintage_cycle_utc']}")
        print(f"forecast_hours={result['forecast_hours']}")
        print(f"n_members={result['n_members']}")
        print(f"snapshot_margin_min_p90={result['snapshot_margin_min_p90']:.1f}")
        print(
            f"grid_lat={result.get('grid_lat')} grid_lon={result.get('grid_lon')} "
            f"distance_km={result.get('grid_distance_km')}"
        )
        print("\n=== member daily max (°F) from instantaneous 3-h TMP ===")
        for item in result["members"]:
            print(f"  {item['member']}  {item['tmax_f']:.2f} F  n_hours={item['n_hours']}")
        cycle = datetime.fromisoformat(result["vintage_cycle_utc"])
        idx_url = self._idx_url(cycle, "gec00", int(result["forecast_hours"][0]))
        _status, idx_text = self.http.fetch_text(idx_url)
        lines = parse_idx_text(idx_text)
        tmp = select_tmp_2m_lines(lines)
        tmax = select_tmax_2m_lines(lines)
        print("\n=== matched idx (control, first climate-day f-hour) ===")
        for line in tmp:
            print(f"TMP  {line.raw_line}")
        for line in tmax:
            print(f"TMAX {line.raw_line} accumulating={line.is_accumulating_tmax}")
        return 0

    def dry_run(self) -> int:
        dates = self.sampled_climate_dates()
        if not dates:
            print("nbm_sample_dir is empty; cannot size the 300-day extract")
            return 1
        latency = self.latency_min
        print("=== GEFS dry-run (idx byte ranges only; no grib) ===")
        print(f"n_days={len(dates)} n_members={len(MEMBERS)}")
        print(f"publication_latency_min={latency}")
        sample = dates[0]
        if latency is None:
            cycle = datetime(
                sample.year, sample.month, sample.day, 18, tzinfo=timezone.utc
            ) - timedelta(days=1)
            hours = forecast_hours_for_climate_day(cycle, sample)
            print(
                "NOTE: latency unset; byte estimate uses D-1 18Z for sizing only. "
                "This is not a vintage."
            )
        else:
            snapshot = snapshot_utc_for_climate_date(sample, self.horizon_h)
            vintage = select_vintage(
                sample,
                snapshot,
                latency_p90_min=latency,
                latency_max_min=int(self.latency_max_min or latency),
            )
            cycle = vintage.cycle
            hours = list(vintage.forecast_hours)
        idx_url = self._idx_url(cycle, "gec00", hours[0])
        status, idx_text = self.http.fetch_text(idx_url)
        if status != 200:
            print(f"idx HTTP {status} {idx_url}")
            return 1
        lines = parse_idx_text(idx_text)
        tmp = select_tmp_2m_lines(lines)
        ranges = byte_ranges_for_gefs_lines(lines, tmp)
        msg_bytes = ranges[0].size if ranges else 0
        n_messages = len(dates) * len(MEMBERS) * len(hours)
        total = n_messages * msg_bytes
        gb = total / (1024**3)
        print(f"tmp_message_bytes={msg_bytes} f_hours={hours}")
        print(f"n_messages={n_messages} estimated_grib_bytes={total} ({gb:.2f} GiB)")
        if gb > BYTE_CAP_GB:
            print(f"STOP: estimate {gb:.2f} GiB exceeds {BYTE_CAP_GB:.0f} GiB cap")
            return 2
        print("under byte cap; bulk still waits on pinned p90 + root-chat OK")
        return 0

    def backfill(self) -> int:
        require_latency(self.latency_min)
        for climate_date in self.sampled_climate_dates():
            key = climate_date.isoformat()
            if gefs_day_complete(self.conn, key):
                continue
            result = self.process_climate_date(climate_date)
            if result.get("status") == "ok":
                set_gefs_day_complete(self.conn, key, utc_now_iso())
                logger.info("gefs complete %s bytes=%s", key, result.get("bytes_transferred"))
            else:
                logger.warning("gefs skip %s status=%s", key, result.get("status"))
        return 0


def print_latency_report(rows: list[WatchRow]) -> None:
    witnessed = [
        row
        for row in rows
        if row.status == "complete" and row.first_availability_delta_min is not None
    ]
    pre = [row for row in rows if row.status == "pre_existing"]
    waiting = [row for row in rows if row.status == "waiting"]
    pending = [row for row in rows if row.status == "pending"]
    values = [float(row.first_availability_delta_min or 0.0) for row in witnessed]
    print("=== GEFS latency probe (first HTTP 200, not Last-Modified) ===")
    print(
        f"n_rows={len(rows)} witnessed_first_200={len(witnessed)} "
        f"pre_existing={len(pre)} waiting={len(waiting)} pending={len(pending)}"
    )
    if values:
        print(
            f"median_min={percentile(values, 0.5):.1f} "
            f"p90_min={percentile(values, 0.9):.1f} "
            f"max_min={max(values):.1f}"
        )
        print("Pin publication_latency_min to p90 after root-chat OK. Do not use Last-Modified.")
    else:
        print("no witnessed first-200 yet; keep polling unpublished cycles")
    for row in rows:
        print(
            f"  {row.nominal_cycle_utc.isoformat()} {row.status} "
            f"http={row.http_status_at_first_success} {row.idx_url}"
        )


def run_latency_probe(config: dict[str, Any], *, tick_only: bool, max_wait_hours: float) -> int:
    watch = GefsAvailabilityWatcher(config)
    out_dir = Path(
        str((config.get("gefs_availability_watch") or {}).get("out_dir") or "analysis/out")
    )
    csv_path = out_dir / "gefs_availability_watch.csv"
    try:
        if tick_only:
            out_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            existing = prune_stale_watch_rows(load_watch_rows(csv_path), now=now)
            rows = watch.build_initial_rows(now=now, existing=existing)
            rows = watch.tick(rows, now)
            for row in rows:
                existing[row.key] = row
            existing = prune_stale_watch_rows(existing, now=now)
            save_watch_rows(csv_path, list(existing.values()))
            print_latency_report(list(existing.values()))
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        existing = load_watch_rows(csv_path)
        deadline = datetime.now(timezone.utc) + timedelta(hours=max_wait_hours)
        while datetime.now(timezone.utc) < deadline:
            now = datetime.now(timezone.utc)
            existing = prune_stale_watch_rows(existing, now=now)
            rows = watch.build_initial_rows(now=now, existing=existing)
            rows = watch.tick(rows, now)
            for row in rows:
                existing[row.key] = row
            existing = prune_stale_watch_rows(existing, now=now)
            save_watch_rows(csv_path, list(existing.values()))
            witnessed = sum(
                1
                for row in existing.values()
                if row.status == "complete" and row.first_availability_delta_min is not None
            )
            if witnessed >= watch.min_cycles:
                break
            logger.info("witnessed_first_200=%s / %s", witnessed, watch.min_cycles)
            time.sleep(watch.poll_interval_sec)
        print_latency_report(list(existing.values()))
        return 0
    finally:
        watch.close()


def apply_archive_overrides(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    gefs = dict(config.get("gefs_archive") or {})
    if args.decoded_dir is not None:
        gefs["decoded_dir"] = str(args.decoded_dir)
    if args.backfill_db is not None:
        gefs["backfill_db"] = str(args.backfill_db)
    if args.latency_min is not None:
        gefs["publication_latency_min"] = int(args.latency_min)
        gefs["publication_latency_max_min"] = int(
            args.latency_max_min if args.latency_max_min is not None else args.latency_min
        )
    if args.nbm_sample_dir is not None:
        gefs["nbm_sample_dir"] = str(args.nbm_sample_dir)
    config = dict(config)
    config["gefs_archive"] = gefs
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GEFS archive backfill (byte-range only)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--latency-probe", action="store_true")
    parser.add_argument("--tick", action="store_true", help="single latency poll, no sleep loop")
    parser.add_argument("--max-wait-hours", type=float, default=12.0)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-date", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--decoded-dir", type=Path, default=None)
    parser.add_argument("--backfill-db", type=Path, default=None)
    parser.add_argument("--nbm-sample-dir", type=Path, default=None)
    parser.add_argument("--latency-min", type=int, default=None)
    parser.add_argument("--latency-max-min", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = apply_archive_overrides(load_config(args.config), args)
    if args.latency_probe:
        return run_latency_probe(config, tick_only=args.tick, max_wait_hours=args.max_wait_hours)
    if not (args.probe or args.dry_run or args.backfill):
        parser.error("specify --latency-probe, --probe, --dry-run, or --backfill")
    app = GefsArchiveBackfill(config)
    try:
        if args.probe:
            probe_date = date.fromisoformat(args.probe_date) if args.probe_date else None
            return app.probe(probe_date)
        if args.dry_run:
            return app.dry_run()
        return app.backfill()
    except (LatencyNotPinnedError, VintageAvailabilityError) as exc:
        print(str(exc))
        return 1
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
