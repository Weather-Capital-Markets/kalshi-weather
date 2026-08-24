"""Tests for NBM idx parsing and vintage selection."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ingestion.nbm_idx import (
    VintageAvailabilityError,
    assert_vintage_available,
    available_max_cycles,
    byte_ranges_for_messages,
    byte_ranges_for_selected_lines,
    empirical_vintage_for_climate_date,
    era_band_for_date,
    era_level_count_for_date,
    forecast_lead_hours,
    match_max_window_in_idx,
    max_product_for_cycle_hour,
    nbm_version_for_date,
    parse_idx_text,
    publication_utc,
    select_max_window_percentile_lines,
    vintage_select_cycle,
)


def test_parse_v4_idx_fixture_matches_data_sources_format() -> None:
    text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    assert len(lines) >= 4
    pct = select_max_window_percentile_lines(lines, forecast_hour=18)
    levels = sorted(line.percentile_level for line in pct)
    assert levels == [1, 2, 50, 99]
    assert all(line.window_hours == (0, 18) for line in pct)


def test_f030_12_30_hour_max_window_is_selected() -> None:
    text = (
        "325:453890124:d=2022070400:TMP:2 m above ground:12-30 hour max fcst:1% level\n"
        "326:459291540:d=2022070400:TMP:2 m above ground:12-30 hour max fcst:2% level\n"
        "327:464693853:d=2022070400:TMP:2 m above ground:12-30 hour StdDev fcst:\n"
    )
    lines = parse_idx_text(text)
    pct = select_max_window_percentile_lines(lines, forecast_hour=30)
    assert [line.percentile_level for line in pct] == [1, 2]
    assert pct[0].window_hours == (12, 30)


def test_parse_v5_idx_fixture_21_level_format() -> None:
    text = Path("tests/fixtures/nbm_idx_v5_sample.txt").read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    pct = select_max_window_percentile_lines(lines)
    levels = sorted(line.percentile_level for line in pct)
    assert levels == [0, 5, 50, 100]


def test_byte_range_construction() -> None:
    text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    ranges = byte_ranges_for_messages(lines, file_size=100000)
    assert ranges[0].start == 0
    assert ranges[0].end == 12344
    assert ranges[0].header_value() == "bytes=0-12344"
    assert ranges[-1].end == 99999


def test_selected_percentile_ranges_use_full_idx_boundaries() -> None:
    text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")
    all_lines = parse_idx_text(text)
    pct_lines = select_max_window_percentile_lines(all_lines, forecast_hour=18)
    wrong = byte_ranges_for_messages(pct_lines)
    right = byte_ranges_for_selected_lines(all_lines, pct_lines)
    assert wrong[-1].size == 1
    assert right[-1].start == 37035
    assert right[-1].end == 49379
    assert right[-1].size == 12345


def test_era_detection_at_2026_05_04_boundary() -> None:
    assert era_level_count_for_date(date(2026, 5, 3)) == 99
    assert era_level_count_for_date(date(2026, 5, 4)) == 21
    assert era_band_for_date(date(2026, 5, 3)) == "v4_retrospective"
    assert era_band_for_date(date(2026, 5, 4)) == "v5_prospective"
    assert nbm_version_for_date(date(2026, 5, 3)) == "v4.3"
    assert nbm_version_for_date(date(2026, 5, 4)) == "v5.0"


def test_vintage_select_respects_publication_latency() -> None:
    # Snapshot 20 min after 12Z nominal must NOT select 12Z under 60 min latency.
    snapshot = datetime(2022, 7, 4, 12, 20, tzinfo=timezone.utc)
    candidates = [datetime(2022, 7, 4, 12, 0, tzinfo=timezone.utc)]
    selected = vintage_select_cycle(snapshot, candidates, latency_min=60)
    assert selected is None
    pub = publication_utc(candidates[0], 60)
    assert pub == datetime(2022, 7, 4, 13, 0, tzinfo=timezone.utc)
    assert pub >= snapshot


def test_vintage_select_picks_latest_valid_cycle() -> None:
    snapshot = datetime(2022, 7, 4, 14, 0, tzinfo=timezone.utc)
    candidates = [
        datetime(2022, 7, 4, 12, 0, tzinfo=timezone.utc),
        datetime(2022, 7, 4, 11, 0, tzinfo=timezone.utc),
    ]
    selected = vintage_select_cycle(snapshot, candidates, latency_min=60)
    assert selected == datetime(2022, 7, 4, 12, 0, tzinfo=timezone.utc)


def test_max_product_alternation_12z_f018_is_max() -> None:
    assert max_product_for_cycle_hour(12, 18) is True
    assert max_product_for_cycle_hour(0, 18) is False
    assert max_product_for_cycle_hour(0, 30) is True
    assert max_product_for_cycle_hour(12, 30) is False


def test_t24h_vintage_is_prior_day_12z_at_441min_latency() -> None:
    from ingestion.nbm_archive import snapshot_utc_for_climate_date

    climate = date(2022, 7, 4)
    snapshot = snapshot_utc_for_climate_date(climate, 24)
    assert snapshot == datetime(2022, 7, 4, 5, 0, tzinfo=timezone.utc)
    candidates = available_max_cycles(snapshot, latency_max_min=453)
    selected = vintage_select_cycle(snapshot, candidates, latency_min=441)
    assert selected == datetime(2022, 7, 3, 12, 0, tzinfo=timezone.utc)
    idx_text = Path("tests/fixtures/nbm_idx_f042_12z_sample.txt").read_text(encoding="utf-8")
    matched = match_max_window_in_idx(
        parse_idx_text(idx_text),
        cycle_dt=selected,
        climate_date=climate,
        percentile_levels={10, 20, 50, 90},
    )
    assert matched is not None
    assert matched.forecast_hour == 42
    assert forecast_lead_hours(selected, climate) == 24.0


def test_assert_vintage_available_rejects_boundary_cycle() -> None:
    snapshot = datetime(2022, 7, 4, 5, 0, tzinfo=timezone.utc)
    cycle = datetime(2022, 7, 3, 12, 0, tzinfo=timezone.utc)
    pub = publication_utc(cycle, 441)
    assert pub < snapshot
    assert_vintage_available(cycle, snapshot, latency_p90_min=441)
    too_late = datetime(2022, 7, 4, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(VintageAvailabilityError):
        assert_vintage_available(too_late, snapshot, latency_p90_min=441)


def test_match_f042_idx_fixture_for_climate_day() -> None:
    climate = date(2022, 7, 4)
    cycle = datetime(2022, 7, 3, 12, tzinfo=timezone.utc)
    idx_text = Path("tests/fixtures/nbm_idx_f042_12z_sample.txt").read_text(encoding="utf-8")
    matched = match_max_window_in_idx(
        parse_idx_text(idx_text),
        cycle_dt=cycle,
        climate_date=climate,
    )
    assert matched is not None
    assert matched.forecast_hour == 42
    assert len(matched.idx_lines) == 4


def test_empirical_vintage_uses_idx_callback() -> None:
    climate = date(2022, 7, 4)
    snapshot = datetime(2022, 7, 4, 5, 0, tzinfo=timezone.utc)
    idx_text = Path("tests/fixtures/nbm_idx_f042_12z_sample.txt").read_text(encoding="utf-8")

    def fetch_idx(cycle: datetime, forecast_hour: int) -> str | None:
        if cycle == datetime(2022, 7, 3, 12, tzinfo=timezone.utc) and forecast_hour == 42:
            return idx_text
        return None

    selection = empirical_vintage_for_climate_date(
        climate,
        snapshot,
        fetch_idx=fetch_idx,
        latency_p90_min=441,
        latency_max_min=453,
        percentile_levels={10, 20, 50, 90},
    )
    assert selection is not None
    assert selection.forecast_hour == 42
    assert selection.cycle == datetime(2022, 7, 3, 12, tzinfo=timezone.utc)
