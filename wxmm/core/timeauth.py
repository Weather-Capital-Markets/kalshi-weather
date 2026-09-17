"""Single time authority for wxmm.

Allowed to assume
    Climate day is midnight-to-midnight **local standard time year-round**
    (NWS CLI; ``knowledge/data-sources.md`` §1.1). For ``KNYC`` and ``KLGA``
    that is UTC−5. During EDT the civil observation day runs 1 AM–1 AM local.
    A max at 12:40 AM EDT belongs to the previous climate day.

Must never
    Be bypassed. No other wxmm module may import ``ZoneInfo`` or do date
    arithmetic on market data. Must never use ``America/New_York`` as the
    climate-day cut (DST would silently shift the day). Must never call
    ``datetime.now`` / ``time.time``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from wxmm.core.utc import require_utc

UTC = timezone.utc

# Local standard time for Eastern US climate stations. Fixed offset; not civil TZ.
_EASTERN_LST = timezone(timedelta(hours=-5))
_EASTERN_CIVIL = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class StationTimeSpec:
    """Timezone facts for a weather station. LST offset is year-round."""

    station: str
    iana_civil: str
    lst: timezone


_STATIONS: dict[str, StationTimeSpec] = {
    "KNYC": StationTimeSpec("KNYC", "America/New_York", _EASTERN_LST),
    "KLGA": StationTimeSpec("KLGA", "America/New_York", _EASTERN_LST),
}


def register_station(spec: StationTimeSpec) -> None:
    """Accept one additional station. Does not invent its climate-day rule."""
    if spec.station in _STATIONS:
        existing = _STATIONS[spec.station]
        if existing != spec:
            raise ValueError(f"station {spec.station!r} already registered with a different spec")
        return
    _STATIONS[spec.station] = spec


def station_spec(station: str) -> StationTimeSpec:
    try:
        return _STATIONS[station]
    except KeyError as exc:
        raise KeyError(
            f"unknown station {station!r}; register via timeauth.register_station"
        ) from exc


def to_utc(ts: datetime) -> datetime:
    """Convert any aware datetime to UTC. Naive values are rejected."""
    return require_utc(ts)


def climate_day(ts: datetime, station: str) -> date:
    """LST calendar date containing ``ts`` at ``station``."""
    spec = station_spec(station)
    aware = ts if ts.tzinfo is not None else (_raise_naive())
    return aware.astimezone(spec.lst).date()


def climate_day_bounds(climate_date: date, station: str) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` of the climate day, returned in UTC."""
    spec = station_spec(station)
    start_lst = datetime(
        climate_date.year,
        climate_date.month,
        climate_date.day,
        tzinfo=spec.lst,
    )
    end_lst = start_lst + timedelta(days=1)
    return start_lst.astimezone(UTC), end_lst.astimezone(UTC)


def is_dst(ts: datetime, station: str) -> bool:
    """Whether civil time at ``station`` is in daylight saving at ``ts``."""
    spec = station_spec(station)
    civil_tz = ZoneInfo(spec.iana_civil)
    civil = (ts if ts.tzinfo is not None else _raise_naive()).astimezone(civil_tz)
    dst = civil.dst()
    return dst is not None and dst != timedelta(0)


def localize_civil(
    station: str,
    on: date,
    *,
    hour: int,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Interpret wall-clock *civil* time at ``station`` and return UTC.

    Use for venue phrases such as \"10:00 AM ET\". Do not use for climate-day
    cuts (those are LST via ``climate_day`` / ``climate_day_bounds``).
    """
    spec = station_spec(station)
    civil_tz = ZoneInfo(spec.iana_civil)
    local = datetime(on.year, on.month, on.day, hour, minute, second, tzinfo=civil_tz)
    return local.astimezone(UTC)


def localize_lst(
    station: str,
    on: date,
    *,
    hour: int,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Interpret local-standard-time clock at ``station`` and return UTC."""
    spec = station_spec(station)
    local = datetime(on.year, on.month, on.day, hour, minute, second, tzinfo=spec.lst)
    return local.astimezone(UTC)


def _raise_naive() -> datetime:
    raise ValueError("naive datetime rejected; pass an aware timestamp")
