"""Climate-day assignment for KNYC / CLINYC.

The climate-day *boundary* is local standard time year-round (UTC-5 for New York).
Do not use America/New_York for that cut — DST would silently shift the day.

zoneinfo / named offsets (EDT/EST) are only for converting CLINYC issuance
headers to UTC. See knowledge/data-sources.md §1.1.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

ClimateDstStatus = Literal["edt", "est", "transition"]

LST = timezone(timedelta(hours=-5))
NY_CIVIL = ZoneInfo("America/New_York")

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def climate_date_of(dt: datetime) -> date:
    """Return the LST calendar date that contains *dt*."""
    if dt.tzinfo is None:
        raise ValueError("climate_date_of requires an aware datetime")
    return dt.astimezone(LST).date()


def climate_day_start(climate_date: date) -> datetime:
    return datetime(
        climate_date.year,
        climate_date.month,
        climate_date.day,
        tzinfo=LST,
    )


def climate_day_end(climate_date: date) -> datetime:
    return climate_day_start(climate_date) + timedelta(days=1)


def climate_dst_status(climate_date: str | date) -> ClimateDstStatus:
    """Classify a climate day for Clock B / last-trading-time identification.

    EDT days: LST and civil (LDT) offsets disagree — hypotheses are separable.
    EST days: offsets agree — LST vs LDT map the same instant (non-identifying).
    Transition days: offset changes inside the LST day; excluded from EDT pools.
    """
    if isinstance(climate_date, str):
        day = date.fromisoformat(climate_date)
    else:
        day = climate_date
    start = climate_day_end(day) - timedelta(days=1)
    end = climate_day_end(day)
    if start.astimezone(NY_CIVIL).utcoffset() != end.astimezone(NY_CIVIL).utcoffset():
        return "transition"
    midday = datetime(day.year, day.month, day.day, 12, 0, tzinfo=LST)
    if midday.astimezone(NY_CIVIL).utcoffset() != midday.utcoffset():
        return "edt"
    return "est"


def parse_nws_issuance_ts(header_time: str) -> datetime | None:
    """Parse '220 AM EDT SUN JUL 05 2026' into an aware UTC datetime.

    Named offsets (EDT/EST) go through America/New_York. This must not be used
    as the climate-day boundary.
    """
    text = " ".join(header_time.upper().split())
    parts = text.split()
    if len(parts) < 6:
        return None
    clock, ampm, tzname, _dow, month_s, day_s, *rest = parts
    if not rest:
        return None
    year_s = rest[0]
    month = MONTHS.get(month_s[:3])
    if month is None or ampm not in {"AM", "PM"}:
        return None
    try:
        day = int(day_s)
        year = int(year_s)
    except ValueError:
        return None
    digits = "".join(ch for ch in clock if ch.isdigit())
    if not digits:
        return None
    if len(digits) <= 2:
        hour, minute = int(digits), 0
    else:
        hour, minute = int(digits[:-2]), int(digits[-2:])
    hour = hour % 12
    if ampm == "PM":
        hour += 12
    naive = datetime(year, month, day, hour, minute)
    # Fold is irrelevant for issuance headers; they are wall-clock civil time.
    civil = naive.replace(tzinfo=NY_CIVIL)
    return civil.astimezone(timezone.utc)


def parse_lst_clock(raw: str) -> tuple[int, int] | None:
    """Parse a TIME-column clock like '455 PM' or '1140 PM' as hour:minute.

    Returned hour is 0-23 on a 24-hour clock with no timezone attached.
    The column may be LST or LDT in summer; callers must not assume which.
    """
    text = " ".join(raw.upper().replace(":", "").split())
    parts = text.split()
    if len(parts) < 2:
        return None
    digits, ampm = parts[0], parts[1]
    if ampm not in {"AM", "PM"}:
        return None
    if not digits.isdigit():
        return None
    if len(digits) <= 2:
        hour, minute = int(digits), 0
    else:
        hour, minute = int(digits[:-2]), int(digits[-2:])
    hour = hour % 12
    if ampm == "PM":
        hour += 12
    return hour, minute
