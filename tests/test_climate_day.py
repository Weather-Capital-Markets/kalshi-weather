"""Climate-day LST assignment tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ingestion.climate_day import climate_date_of, parse_lst_clock, parse_nws_issuance_ts

NY = ZoneInfo("America/New_York")


def test_dst_early_morning_belongs_to_previous_lst_day() -> None:
    # 00:30 EDT 2026-07-04 = 23:30 EST 2026-07-03.
    dt = datetime(2026, 7, 4, 0, 30, tzinfo=NY)
    assert climate_date_of(dt).isoformat() == "2026-07-03"


def test_max_just_after_midnight_edt_is_previous_climate_day() -> None:
    dt = datetime(2026, 7, 4, 0, 40, tzinfo=NY)
    assert climate_date_of(dt).isoformat() == "2026-07-03"


def test_issuance_edt_converts_to_utc_without_moving_day_cut() -> None:
    ts = parse_nws_issuance_ts("220 AM EDT SUN JUL 05 2026")
    assert ts is not None
    assert ts.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-07-05T06:20:00Z"
    # 2:20 AM EDT = 1:20 AM LST: still the 5th. The day cut only moves for
    # clock times before 1:00 AM EDT (midnight LST).
    local_edt = datetime(2026, 7, 5, 2, 20, tzinfo=NY)
    assert climate_date_of(local_edt).isoformat() == "2026-07-05"


def test_parse_lst_clock_is_timezone_naive() -> None:
    assert parse_lst_clock("455 PM") == (16, 55)
    assert parse_lst_clock("105 PM") == (13, 5)
    assert parse_lst_clock("12:40 AM") == (0, 40)
