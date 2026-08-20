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
MAX_WINDOW_STAT = "0-18 hour max fcst"

# f018, f030, … f270 — multiples of 6 with f ≡ 6 (mod 12).
QMD_WINDOW_FORECAST_HOURS: tuple[int, ...] = tuple(range(18, 271, 12))


@dataclass(frozen=True)
class IdxLine:
    msg_number: int
    byte_offset: int
    cycle_tag: str
    window_stat: str
    tail: str
    raw_line: str

    @property
    def is_max_window_percentile(self) -> bool:
        return self.window_stat == MAX_WINDOW_STAT and self.percentile_level is not None

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


def select_max_window_percentile_lines(lines: list[IdxLine]) -> list[IdxLine]:
    return [line for line in lines if line.is_max_window_percentile]


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


def kelvin_to_fahrenheit(value: float) -> float:
    return (value - 273.15) * 9.0 / 5.0 + 32.0


EraBand = Literal["v4_retrospective", "v5_prospective"]

def era_band_for_date(climate_date: date) -> EraBand:
    if climate_date < date(2026, 5, 4):
        return "v4_retrospective"
    return "v5_prospective"
