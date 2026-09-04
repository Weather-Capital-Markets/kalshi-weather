"""Climate-day assignment via the single time authority."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from wxmm.core.timeauth import (
    climate_day,
    climate_day_bounds,
    is_dst,
    localize_civil,
    localize_lst,
    to_utc,
)

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
STATION = "KNYC"


def test_max_at_1240_am_edt_belongs_to_previous_climate_day() -> None:
    ts = datetime(2026, 7, 4, 0, 40, tzinfo=NY)
    assert climate_day(ts, STATION) == date(2026, 7, 3)


def test_max_at_1240_am_est_belongs_to_same_climate_day() -> None:
    ts = datetime(2026, 1, 15, 0, 40, tzinfo=NY)
    assert climate_day(ts, STATION) == date(2026, 1, 15)


def test_dst_spring_forward_does_not_shift_lst_cut() -> None:
    # 2026-03-08 02:00 EST → 03:00 EDT. 00:40 is still EST = 00:40 LST.
    before = datetime(2026, 3, 8, 0, 40, tzinfo=NY)
    assert climate_day(before, STATION) == date(2026, 3, 8)
    after = datetime(2026, 3, 8, 3, 30, tzinfo=NY)
    assert climate_day(after, STATION) == date(2026, 3, 8)
    assert is_dst(before, STATION) is False
    assert is_dst(after, STATION) is True


def test_dst_fall_back_1240_am_edt_is_previous_climate_day() -> None:
    # 2026-11-01 02:00 EDT → 01:00 EST. 00:40 is still EDT = 23:40 LST Oct 31.
    ts = datetime(2026, 11, 1, 0, 40, tzinfo=NY)
    assert climate_day(ts, STATION) == date(2026, 10, 31)
    assert is_dst(ts, STATION) is True
    evening = datetime(2026, 11, 1, 3, 0, tzinfo=NY)
    assert climate_day(evening, STATION) == date(2026, 11, 1)
    assert is_dst(evening, STATION) is False


def test_klga_shares_eastern_lst_but_is_a_distinct_station() -> None:
    ts = datetime(2026, 7, 4, 0, 40, tzinfo=NY)
    assert climate_day(ts, "KLGA") == date(2026, 7, 3)


def test_unknown_station_is_rejected() -> None:
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    with pytest.raises(KeyError, match="unknown station"):
        climate_day(ts, "KORD")


def test_climate_day_bounds_are_utc_half_open() -> None:
    start, end = climate_day_bounds(date(2026, 7, 4), STATION)
    assert start == datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 5, 5, 0, tzinfo=UTC)
    assert climate_day(start, STATION) == date(2026, 7, 4)
    just_before_end = end.replace(microsecond=1) - timedelta(microseconds=2)
    assert climate_day(just_before_end, STATION) == date(2026, 7, 4)
    assert climate_day(end, STATION) == date(2026, 7, 5)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 7, 4, 12, 0))


def test_localize_civil_10am_et_summer() -> None:
    ts = localize_civil(STATION, date(2026, 7, 5), hour=10, minute=0)
    assert ts == datetime(2026, 7, 5, 14, 0, tzinfo=UTC)


def test_localize_lst_midnight() -> None:
    ts = localize_lst(STATION, date(2026, 7, 4), hour=0, minute=0)
    assert ts == datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
