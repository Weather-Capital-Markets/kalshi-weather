"""Tests for NBM idx parsing and vintage selection."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from ingestion.nbm_idx import (
    byte_ranges_for_messages,
    era_band_for_date,
    era_level_count_for_date,
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
    pct = select_max_window_percentile_lines(lines)
    levels = sorted(line.percentile_level for line in pct)
    assert levels == [1, 2, 50, 99]
    assert all(line.window_stat == "0-18 hour max fcst" for line in pct)


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
