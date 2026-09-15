"""Clock interpretation for CLINYC time-of-high and ASOS daily maxima.

The climate-day boundary is always LST (see climate_day.py). This module handles
how the printed TIME-column clock is mapped to an instant, and how ASOS hourly
observations are reduced to a daily maximum over that LST day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from ingestion.climate_day import LST, NY_CIVIL, climate_day_end, climate_day_start, parse_lst_clock

CliTimeConvention = Literal["lst", "ldt", "unknown"]


@dataclass(frozen=True)
class AsosObservation:
    valid_utc: datetime
    tmpf: float


def load_cli_time_convention(config: dict[str, Any]) -> CliTimeConvention:
    raw = str((config.get("climate") or {}).get("cli_time_convention") or "unknown").lower()
    if raw in ("lst", "ldt", "unknown"):
        return raw  # type: ignore[return-value]
    return "unknown"


def cli_max_instant(
    climate_date: str,
    time_of_high_raw: str,
    convention: CliTimeConvention,
) -> datetime | None:
    """Map a printed CLI time-of-high to an aware instant on the climate date."""
    if convention == "unknown":
        return None
    parsed = parse_lst_clock(str(time_of_high_raw or ""))
    if parsed is None:
        return None
    hour, minute = parsed
    year, month, day = (int(part) for part in climate_date.split("-"))
    if convention == "lst":
        return datetime(year, month, day, hour, minute, tzinfo=LST)
    naive = datetime(year, month, day, hour, minute)
    return naive.replace(tzinfo=NY_CIVIL)


def asos_max_for_climate_day(
    observations: list[AsosObservation],
    climate_date: str | date,
) -> tuple[float | None, datetime | None]:
    """Return (max tmpf, time of max) over the LST climate day.

  Ties break to the earliest observation.
    """
    if isinstance(climate_date, str):
        day = date.fromisoformat(climate_date)
    else:
        day = climate_date
    start = climate_day_start(day)
    end = climate_day_end(day)
    in_day = [obs for obs in observations if start <= obs.valid_utc < end]
    if not in_day:
        return None, None
    best = max(in_day, key=lambda obs: (obs.tmpf, -obs.valid_utc.timestamp()))
    return best.tmpf, best.valid_utc


def nbm_max_window_utc(climate_date: str | date) -> tuple[datetime, datetime]:
    """NBM 18-h max window: 12Z on climate date through 06Z the following day."""
    if isinstance(climate_date, str):
        day = date.fromisoformat(climate_date)
    else:
        day = climate_date
    window_start = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=18)
    return window_start, window_end


def time_in_window(instant: datetime, window_start: datetime, window_end: datetime) -> bool:
    return window_start <= instant.astimezone(timezone.utc) < window_end
