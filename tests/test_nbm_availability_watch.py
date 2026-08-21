"""Tests for NBM availability watch (no network)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd

from analysis.nbm_availability_watch import (
    availability_delta_minutes,
    enumerate_upcoming_cycles,
    last_modified_gap_minutes,
    load_watch_rows,
    NbmAvailabilityWatcher,
    nomads_idx_url,
    save_watch_rows,
    select_watch_cycles,
    WatchRow,
)


def test_availability_delta_minutes_arithmetic() -> None:
    nominal = datetime(2024, 8, 20, 0, 0, tzinfo=timezone.utc)
    first = datetime(2024, 8, 20, 0, 45, tzinfo=timezone.utc)
    assert availability_delta_minutes(nominal, first) == 45.0


def test_last_modified_gap_diagnostic() -> None:
    gap = last_modified_gap_minutes(45.0, 435.0)
    assert gap == 390.0


def test_enumerate_upcoming_cycles_crosses_day_boundary() -> None:
    from_time = datetime(2024, 8, 20, 23, 30, tzinfo=timezone.utc)
    cycles = enumerate_upcoming_cycles(from_time=from_time, lookback_days=0, lookahead_days=1)
    hours = {nominal.hour for nominal, _ in cycles}
    assert 0 in hours
    assert 18 in hours
    assert any(nominal.date() == date(2024, 8, 21) for nominal, _ in cycles)


def test_select_watch_cycles_covers_cycle_hours() -> None:
    from_time = datetime(2024, 8, 20, 12, 0, tzinfo=timezone.utc)
    picked = select_watch_cycles(from_time=from_time, min_cycles=4, completed_nominals=set())
    hours = {nominal.hour for nominal, _ in picked}
    assert hours == {0, 6, 12, 18}


def test_nomads_idx_url_format() -> None:
    cycle = datetime(2024, 8, 20, 0, tzinfo=timezone.utc)
    url = nomads_idx_url(cycle, 30)
    assert "nomads.ncep.noaa.gov" in url
    assert "blend.20240820/00/qmd/" in url
    assert "f030" in url


def test_poll_mock_404_then_200() -> None:
    config = {
        "nbm_archive": {"base_url": "https://example.com", "publication_latency_min": 60},
        "nbm_availability_watch": {
            "poll_interval_sec": 1,
            "min_cycles": 1,
            "sources": ["aws"],
        },
    }
    watcher = NbmAvailabilityWatcher(config)
    nominal = datetime(2024, 8, 19, 0, 0, tzinfo=timezone.utc)
    row = WatchRow(
        source="aws",
        nominal_cycle_utc=nominal,
        forecast_hour=30,
        idx_url="https://example.com/test.idx",
    )
    watcher.head_idx = MagicMock(return_value=(404, None))
    t1 = datetime(2024, 8, 19, 0, 30, tzinfo=timezone.utc)
    row = watcher.probe_row(row, t1)
    assert row.status == "waiting"
    assert row.first_http_200_utc is None

    watcher.head_idx = MagicMock(
        return_value=(
            200,
            datetime(2024, 8, 19, 7, 12, 34, tzinfo=timezone.utc),
        ),
    )
    t2 = datetime(2024, 8, 19, 0, 50, tzinfo=timezone.utc)
    row = watcher.probe_row(row, t2)
    assert row.status == "complete"
    assert row.first_availability_delta_min == 50.0
    assert row.last_modified_delta_min is not None
    watcher.close()


def test_csv_roundtrip(tmp_path) -> None:
    csv_path = tmp_path / "watch.csv"
    nominal = datetime(2024, 8, 20, 0, 0, tzinfo=timezone.utc)
    row = WatchRow(
        source="aws",
        nominal_cycle_utc=nominal,
        forecast_hour=30,
        idx_url="https://example.com/x.idx",
        status="complete",
        first_http_200_utc=datetime(2024, 8, 20, 0, 40, tzinfo=timezone.utc),
        first_availability_delta_min=40.0,
    )
    save_watch_rows(csv_path, [row])
    loaded = load_watch_rows(csv_path)
    assert len(loaded) == 1
    assert loaded[row.key].first_availability_delta_min == 40.0


def test_tick_appends_new_cycles(tmp_path, monkeypatch) -> None:
    config = {
        "nbm_archive": {"base_url": "https://example.com"},
        "nbm_availability_watch": {
            "min_cycles": 2,
            "sources": ["aws"],
            "poll_interval_sec": 300,
        },
    }
    watcher = NbmAvailabilityWatcher(config)
    watcher.head_idx = MagicMock(return_value=(404, None))
    out_dir = tmp_path / "out"
    code = watcher.run_tick(out_dir)
    assert code == 0
    csv_path = out_dir / "nbm_availability_watch.csv"
    frame = pd.read_csv(csv_path)
    assert len(frame) >= 2
    watcher.close()
