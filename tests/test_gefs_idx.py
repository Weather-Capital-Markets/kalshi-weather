"""GEFS idx parsing and vintage rejection (no network)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ingestion.gefs_idx import (
    MEMBERS,
    VintageAvailabilityError,
    assert_vintage_available,
    byte_ranges_for_gefs_lines,
    forecast_hours_for_climate_day,
    gefs_idx_url,
    parse_idx_text,
    publication_utc,
    select_tmax_2m_lines,
    select_tmp_2m_lines,
    select_vintage,
    snapshot_utc_for_climate_date,
)

FIXTURE = Path("tests/fixtures/gefs_idx_pgrb2a_f006.txt")


def test_parse_verbatim_gefs_fixture_tmp_and_tmax() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    assert len(lines) == 85
    tmp = select_tmp_2m_lines(lines)
    tmax = select_tmax_2m_lines(lines)
    assert len(tmp) == 1
    assert tmp[0].raw_line.startswith("63:11595236:d=2022121100:TMP:2 m above ground:6 hour fcst:")
    assert len(tmax) == 1
    assert tmax[0].is_accumulating_tmax
    assert tmax[0].forecast == "0-6 hour max fcst"


def test_tmp_byte_range_uses_next_message_boundary() -> None:
    lines = parse_idx_text(FIXTURE.read_text(encoding="utf-8"))
    tmp = select_tmp_2m_lines(lines)
    ranges = byte_ranges_for_gefs_lines(lines, tmp)
    assert len(ranges) == 1
    assert ranges[0].start == 11595236
    assert ranges[0].end == 11831143 - 1
    assert ranges[0].size == 235907


def test_member_count_is_31() -> None:
    assert len(MEMBERS) == 31
    assert MEMBERS[0] == "gec00"
    assert MEMBERS[-1] == "gep30"


def test_forecast_hours_eight_three_hourly_points() -> None:
    climate = date(2022, 12, 15)
    cycle = datetime(2022, 12, 14, 18, tzinfo=timezone.utc)
    hours = forecast_hours_for_climate_day(cycle, climate)
    assert hours == [12, 15, 18, 21, 24, 27, 30, 33]


def test_snapshot_is_t_minus_24h_before_climate_day_end() -> None:
    snapshot = snapshot_utc_for_climate_date(date(2022, 7, 4), 24)
    assert snapshot == datetime(2022, 7, 4, 5, 0, tzinfo=timezone.utc)


def test_vintage_rejects_cycle_publishing_one_minute_after_snapshot() -> None:
    snapshot = datetime(2022, 12, 15, 5, 0, tzinfo=timezone.utc)
    cycle = datetime(2022, 12, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(VintageAvailabilityError):
        assert_vintage_available(cycle, snapshot, latency_p90_min=301)


def test_vintage_accepts_cycle_publishing_one_minute_before_snapshot() -> None:
    snapshot = datetime(2022, 12, 15, 5, 0, tzinfo=timezone.utc)
    cycle = datetime(2022, 12, 15, 0, 0, tzinfo=timezone.utc)
    pub = assert_vintage_available(cycle, snapshot, latency_p90_min=299)
    assert pub == publication_utc(cycle, 299)
    assert pub < snapshot


def test_select_vintage_skips_illegal_00z_when_p90_exceeds_five_hours() -> None:
    climate = date(2022, 12, 15)
    snapshot = snapshot_utc_for_climate_date(climate, 24)
    vintage = select_vintage(
        climate,
        snapshot,
        latency_p90_min=360,
        latency_max_min=360,
    )
    assert vintage.cycle == datetime(2022, 12, 14, 18, tzinfo=timezone.utc)
    assert vintage.forecast_hours == (12, 15, 18, 21, 24, 27, 30, 33)
    assert vintage.snapshot_margin_min_p90 > 0


def test_idx_url_layout() -> None:
    cycle = datetime(2022, 12, 11, 0, tzinfo=timezone.utc)
    url = gefs_idx_url("https://noaa-gefs-pds.s3.amazonaws.com", cycle, "gec00", 6)
    assert url.endswith("gefs.20221211/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f006.idx")
