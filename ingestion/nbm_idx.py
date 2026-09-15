"""Parse NBM qmd .idx sidecars and build HTTP byte-range requests.

Per data-sources.md §3.2 — window max/min temperature is TMP with a window
statistic; idx lines are the parser contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

IDX_LINE_RE = re.compile(
    r"^(?P<msg>\d+):(?P<byte_offset>\d+):"
    r"d=(?P<cycle>\d{10}):"
    r"TMP:2 m above ground:"
    r"(?P<window_stat>[^:]+):"
    r"(?P<tail>.*)$"
)

PERCENTILE_TAIL_RE = re.compile(r"^P?(\d+)% level$")
WINDOW_STAT_RE = re.compile(r"^(?P<start>\d+)-(?P<end>\d+) hour max fcst$")

# f018, f030, … f270 — multiples of 6 with f ≡ 6 (mod 12).
QMD_WINDOW_FORECAST_HOURS: tuple[int, ...] = tuple(range(18, 271, 12))
# Window max/min qmd products exist on 00Z and 12Z cycles only (§3.2).
MAX_PRODUCT_CYCLE_HOURS: tuple[int, ...] = (0, 12)


@dataclass(frozen=True)
class IdxLine:
    msg_number: int
    byte_offset: int
    cycle_tag: str
    window_stat: str
    tail: str
    raw_line: str

    @property
    def window_hours(self) -> tuple[int, int] | None:
        match = WINDOW_STAT_RE.match(self.window_stat)
        if not match:
            return None
        return int(match.group("start")), int(match.group("end"))

    @property
    def is_max_window_percentile(self) -> bool:
        hours = self.window_hours
        if hours is None:
            return False
        start_h, end_h = hours
        return (end_h - start_h) == 18 and self.percentile_level is not None

    @property
    def percentile_level(self) -> int | None:
        if not self.tail:
            return None
        match = PERCENTILE_TAIL_RE.match(self.tail.strip())
        if not match:
            return None
        return int(match.group(1))


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    idx_line: IdxLine

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def header_value(self) -> str:
        return f"bytes={self.start}-{self.end}"


def parse_idx_line(line: str) -> IdxLine | None:
    text = line.strip()
    if not text:
        return None
    match = IDX_LINE_RE.match(text)
    if not match:
        return None
    return IdxLine(
        msg_number=int(match.group("msg")),
        byte_offset=int(match.group("byte_offset")),
        cycle_tag=match.group("cycle"),
        window_stat=match.group("window_stat").strip(),
        tail=match.group("tail").strip(),
        raw_line=text,
    )


def parse_idx_text(text: str) -> list[IdxLine]:
    lines: list[IdxLine] = []
    for raw in text.splitlines():
        parsed = parse_idx_line(raw)
        if parsed is not None:
            lines.append(parsed)
    return lines


def byte_ranges_for_messages(
    lines: list[IdxLine],
    *,
    file_size: int | None = None,
) -> list[ByteRange]:
    """Compute inclusive byte ranges for each message in idx order."""
    if not lines:
        return []
    ranges: list[ByteRange] = []
    for idx, line in enumerate(lines):
        start = line.byte_offset
        if idx + 1 < len(lines):
            end = lines[idx + 1].byte_offset - 1
        elif file_size is not None and file_size > start:
            end = file_size - 1
        else:
            end = start
        ranges.append(ByteRange(start=start, end=end, idx_line=line))
    return ranges


def byte_ranges_for_selected_lines(
    all_lines: list[IdxLine],
    selected_lines: list[IdxLine],
    *,
    file_size: int | None = None,
) -> list[ByteRange]:
    """Byte ranges for a subset of idx lines using full-file boundaries.

    Filtering to percentile lines before range construction yields wrong ends
    whenever non-selected messages sit between selected ones (typical in NBM
    qmd idx files).
    """
    if not selected_lines:
        return []
    all_ranges = byte_ranges_for_messages(all_lines, file_size=file_size)
    by_offset = {item.idx_line.byte_offset: item for item in all_ranges}
    ranges: list[ByteRange] = []
    for line in selected_lines:
        found = by_offset.get(line.byte_offset)
        if found is not None:
            ranges.append(found)
    return ranges


def select_max_window_percentile_lines(
    lines: list[IdxLine],
    *,
    forecast_hour: int | None = None,
    percentile_levels: set[int] | None = None,
) -> list[IdxLine]:
    selected = [line for line in lines if line.is_max_window_percentile]
    if forecast_hour is not None:
        selected = [
            line
            for line in selected
            if line.window_hours is not None and line.window_hours[1] == forecast_hour
        ]
    if percentile_levels is not None:
        selected = [
            line
            for line in selected
            if line.percentile_level is not None and line.percentile_level in percentile_levels
        ]
    return selected


def era_level_count_for_date(climate_date: date) -> int:
    """Hard segment boundary at 2026-05-04 per data-sources.md §3.3."""
    if climate_date < date(2026, 5, 4):
        return 99
    return 21


def nbm_version_for_date(climate_date: date) -> str:
    """Archive-empirical era tags from data-sources.md §3.1."""
    if climate_date < date(2020, 9, 29):
        return "v3.2"
    if climate_date < date(2023, 1, 17):
        return "v4.0"
    if climate_date < date(2024, 5, 15):
        return "v4.1"
    if climate_date < date(2025, 4, 15):
        return "v4.2"
    if climate_date < date(2026, 5, 4):
        return "v4.3"
    return "v5.0"


def cycle_nominal_utc(cycle_tag: str) -> datetime:
    """Parse d=YYYYMMDDCC from idx line or cycle key."""
    text = cycle_tag
    if text.startswith("d="):
        text = text[2:]
    if len(text) < 10:
        raise ValueError(f"invalid cycle tag: {cycle_tag!r}")
    year = int(text[0:4])
    month = int(text[4:6])
    day = int(text[6:8])
    hour = int(text[8:10])
    return datetime(year, month, day, hour, 0, tzinfo=timezone.utc)


def publication_utc(nominal: datetime, latency_min: int) -> datetime:
    return nominal + timedelta(minutes=latency_min)


def forecast_hour_from_filename(filename: str) -> int | None:
    """Extract fXXX from blend.tCCz.qmd.fXXX.co.grib2."""
    match = re.search(r"\.f(\d{3})\.", filename)
    if not match:
        return None
    return int(match.group(1))


def is_window_forecast_hour(forecast_hour: int) -> bool:
    return forecast_hour in QMD_WINDOW_FORECAST_HOURS


def max_product_for_cycle_hour(cycle_hour: int, forecast_hour: int) -> bool:
    """Max/min alternate by cycle per data-sources.md §3.2."""
    offset = forecast_hour - 18
    if offset < 0 or offset % 12 != 0:
        return False
    if offset % 24 == 0:
        return cycle_hour == 12
    return cycle_hour == 0


def vintage_select_cycle(
    snapshot_utc: datetime,
    candidates: list[datetime],
    *,
    latency_min: int,
) -> datetime | None:
    """Return latest nominal cycle with publication strictly before snapshot."""
    valid: list[datetime] = []
    for nominal in candidates:
        pub = publication_utc(nominal, latency_min)
        if pub < snapshot_utc:
            valid.append(nominal)
    if not valid:
        return None
    return max(valid)


def candidate_cycles_for_snapshot(snapshot_utc: datetime) -> list[datetime]:
    """Hourly cycles on snapshot day and previous day (archive is hourly)."""
    candidates: list[datetime] = []
    day_start = datetime(
        snapshot_utc.year,
        snapshot_utc.month,
        snapshot_utc.day,
        tzinfo=timezone.utc,
    ) - timedelta(days=1)
    for hour_offset in range(48):
        candidates.append(day_start + timedelta(hours=hour_offset))
    return [c for c in candidates if c < snapshot_utc]


def candidate_max_cycles_for_snapshot(snapshot_utc: datetime) -> list[datetime]:
    """00Z/12Z cycles only — qmd 18-h max/min is not on off-hour blends."""
    return [
        cycle
        for cycle in candidate_cycles_for_snapshot(snapshot_utc)
        if cycle.hour in MAX_PRODUCT_CYCLE_HOURS
    ]


def max_window_end_utc(cycle_dt: datetime, forecast_hour: int) -> datetime:
    """Valid time of a 0–18h window product: cycle + forecast hour."""
    return cycle_dt + timedelta(hours=forecast_hour)


def forecast_hour_for_climate_max_window(
    cycle_dt: datetime,
    climate_date: date,
) -> int | None:
    """Return the f-hour whose 18-h max window is 12Z climate_date → 06Z next day."""
    window_end = datetime(
        climate_date.year,
        climate_date.month,
        climate_date.day,
        12,
        tzinfo=timezone.utc,
    ) + timedelta(hours=18)
    if cycle_dt.hour not in MAX_PRODUCT_CYCLE_HOURS:
        return None
    for forecast_hour in QMD_WINDOW_FORECAST_HOURS:
        if not max_product_for_cycle_hour(cycle_dt.hour, forecast_hour):
            continue
        if max_window_end_utc(cycle_dt, forecast_hour) == window_end:
            return forecast_hour
    return None


def kelvin_to_fahrenheit(value: float) -> float:
    return (value - 273.15) * 9.0 / 5.0 + 32.0


EraBand = Literal["v4_retrospective", "v5_prospective"]

def era_band_for_date(climate_date: date) -> EraBand:
    if climate_date < date(2026, 5, 4):
        return "v4_retrospective"
    return "v5_prospective"
