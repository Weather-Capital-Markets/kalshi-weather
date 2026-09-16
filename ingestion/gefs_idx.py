"""Parse GEFS pgrb2a .idx sidecars and select 2 m temperature byte ranges.

Operational AWS layout (verified 2022-12-11 and 2026-05-03):
  gefs.YYYYMMDD/CC/atmos/pgrb2ap5/{gec00|gepNN}.tCCz.pgrb2a.0p50.fHHH.idx

Cadence at these leads is 3-hourly (f000, f003, …). TMAX messages are either
accumulating from cycle start (`0-N hour max fcst`) or multi-hour period max
(e.g. `6-12 hour max fcst`). Neither is a 3-hourly period max; climate-day
max uses instantaneous TMP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from ingestion.climate_day import climate_day_end, climate_day_start
from ingestion.nbm_idx import (
    VintageAvailabilityError,
    assert_vintage_available,
    publication_utc,
)

IDX_LINE_RE = re.compile(
    r"^(?P<msg>\d+):(?P<byte_offset>\d+):"
    r"d=(?P<cycle>\d{10}):"
    r"(?P<var>[^:]+):(?P<level>[^:]+):"
    r"(?P<fcst>[^:]*):"
    r"(?P<tail>.*)$"
)

MEMBERS: tuple[str, ...] = ("gec00",) + tuple(f"gep{i:02d}" for i in range(1, 31))
CYCLE_HOURS: tuple[int, ...] = (0, 6, 12, 18)
FORECAST_STEP_H = 3
MAX_LEAD_H = 240
LEVEL_2M = "2 m above ground"
TempKind = Literal["tmp", "tmax"]


@dataclass(frozen=True)
class GefsIdxLine:
    msg_number: int
    byte_offset: int
    cycle_tag: str
    var_name: str
    level: str
    forecast: str
    tail: str
    raw_line: str

    @property
    def is_tmp_2m(self) -> bool:
        return self.var_name == "TMP" and self.level.strip() == LEVEL_2M

    @property
    def is_tmax_2m(self) -> bool:
        return self.var_name == "TMAX" and self.level.strip() == LEVEL_2M

    @property
    def is_accumulating_tmax(self) -> bool:
        text = self.forecast.strip()
        return self.is_tmax_2m and text.startswith("0-") and "max fcst" in text


@dataclass(frozen=True)
class GefsByteRange:
    start: int
    end: int
    idx_line: GefsIdxLine

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def header_value(self) -> str:
        return f"bytes={self.start}-{self.end}"


@dataclass(frozen=True)
class GefsVintage:
    cycle: datetime
    forecast_hours: tuple[int, ...]
    publication_utc_p90: datetime
    publication_utc_max: datetime
    snapshot_utc: datetime
    temp_kind: TempKind

    @property
    def snapshot_margin_min_p90(self) -> float:
        return (self.snapshot_utc - self.publication_utc_p90).total_seconds() / 60.0


def parse_idx_line(line: str) -> GefsIdxLine | None:
    text = line.strip()
    if not text:
        return None
    match = IDX_LINE_RE.match(text)
    if not match:
        return None
    return GefsIdxLine(
        msg_number=int(match.group("msg")),
        byte_offset=int(match.group("byte_offset")),
        cycle_tag=match.group("cycle"),
        var_name=match.group("var").strip(),
        level=match.group("level").strip(),
        forecast=match.group("fcst").strip(),
        tail=match.group("tail").strip(),
        raw_line=text,
    )


def parse_idx_text(text: str) -> list[GefsIdxLine]:
    lines: list[GefsIdxLine] = []
    for raw in text.splitlines():
        parsed = parse_idx_line(raw)
        if parsed is not None:
            lines.append(parsed)
    return lines


def select_tmp_2m_lines(lines: list[GefsIdxLine]) -> list[GefsIdxLine]:
    return [line for line in lines if line.is_tmp_2m]


def select_tmax_2m_lines(lines: list[GefsIdxLine]) -> list[GefsIdxLine]:
    return [line for line in lines if line.is_tmax_2m]


def byte_ranges_for_gefs_lines(
    all_lines: list[GefsIdxLine],
    selected: list[GefsIdxLine],
    *,
    file_size: int | None = None,
) -> list[GefsByteRange]:
    """Inclusive ranges using full-file idx boundaries (not the filtered subset)."""
    if not selected:
        return []
    ordered = sorted(all_lines, key=lambda item: item.byte_offset)
    offset_to_end: dict[int, int] = {}
    for idx, line in enumerate(ordered):
        start = line.byte_offset
        if idx + 1 < len(ordered):
            end = ordered[idx + 1].byte_offset - 1
        elif file_size is not None and file_size > start:
            end = file_size - 1
        else:
            end = start
        offset_to_end[start] = end
    ranges: list[GefsByteRange] = []
    for line in selected:
        end = offset_to_end.get(line.byte_offset)
        if end is None:
            continue
        ranges.append(GefsByteRange(start=line.byte_offset, end=end, idx_line=line))
    return ranges


def snapshot_utc_for_climate_date(climate_date: date, horizon_h: int) -> datetime:
    return climate_day_end(climate_date) - timedelta(hours=horizon_h)


def candidate_cycles_for_snapshot(snapshot_utc: datetime) -> list[datetime]:
    """00/06/12/18Z cycles on snapshot day and the two previous days, nominal < snapshot."""
    candidates: list[datetime] = []
    day0 = snapshot_utc.date()
    for offset in range(0, 3):
        day = day0 - timedelta(days=offset)
        for hour in CYCLE_HOURS:
            cycle = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if cycle < snapshot_utc:
                candidates.append(cycle)
    return sorted(candidates)


def available_cycles(
    snapshot_utc: datetime,
    *,
    latency_max_min: int,
) -> list[datetime]:
    return [
        cycle
        for cycle in candidate_cycles_for_snapshot(snapshot_utc)
        if publication_utc(cycle, latency_max_min) < snapshot_utc
    ]


def forecast_hours_for_climate_day(cycle: datetime, climate_date: date) -> list[int]:
    """3-hourly leads whose valid time is inside the LST climate day [start, end)."""
    start = climate_day_start(climate_date)
    end = climate_day_end(climate_date)
    hours: list[int] = []
    lead = 0
    while lead <= MAX_LEAD_H:
        valid = cycle + timedelta(hours=lead)
        if start <= valid < end:
            hours.append(lead)
        lead += FORECAST_STEP_H
    return hours


def select_vintage(
    climate_date: date,
    snapshot_utc: datetime,
    *,
    latency_p90_min: int,
    latency_max_min: int,
    temp_kind: TempKind = "tmp",
) -> GefsVintage:
    """Latest cycle with p90 publication strictly before snapshot and a full f-hour set."""
    candidates = list(reversed(available_cycles(snapshot_utc, latency_max_min=latency_max_min)))
    for cycle in candidates:
        hours = forecast_hours_for_climate_day(cycle, climate_date)
        if len(hours) < 8:
            continue
        pub_p90 = assert_vintage_available(
            cycle,
            snapshot_utc,
            latency_p90_min=latency_p90_min,
        )
        return GefsVintage(
            cycle=cycle,
            forecast_hours=tuple(hours),
            publication_utc_p90=pub_p90,
            publication_utc_max=publication_utc(cycle, latency_max_min),
            snapshot_utc=snapshot_utc,
            temp_kind=temp_kind,
        )
    raise VintageAvailabilityError(
        f"no GEFS cycle available for climate_date={climate_date.isoformat()} "
        f"snapshot={snapshot_utc.isoformat()} latency_p90_min={latency_p90_min}"
    )


def gefs_grib_url(
    base_url: str,
    cycle: datetime,
    member: str,
    forecast_hour: int,
    *,
    product: str = "pgrb2ap5",
    resolution: str = "0p50",
) -> str:
    product_prefix = "pgrb2a" if product.startswith("pgrb2a") else product
    return (
        f"{base_url.rstrip('/')}/gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos/{product}/"
        f"{member}.t{cycle:%H}z.{product_prefix}.{resolution}.f{forecast_hour:03d}"
    )


def gefs_idx_url(
    base_url: str,
    cycle: datetime,
    member: str,
    forecast_hour: int,
    **kwargs: str,
) -> str:
    return gefs_grib_url(base_url, cycle, member, forecast_hour, **kwargs) + ".idx"


__all__ = [
    "CYCLE_HOURS",
    "FORECAST_STEP_H",
    "GefsByteRange",
    "GefsIdxLine",
    "GefsVintage",
    "MEMBERS",
    "VintageAvailabilityError",
    "assert_vintage_available",
    "available_cycles",
    "byte_ranges_for_gefs_lines",
    "candidate_cycles_for_snapshot",
    "forecast_hours_for_climate_day",
    "gefs_grib_url",
    "gefs_idx_url",
    "parse_idx_text",
    "publication_utc",
    "select_tmax_2m_lines",
    "select_tmp_2m_lines",
    "select_vintage",
    "snapshot_utc_for_climate_date",
]
