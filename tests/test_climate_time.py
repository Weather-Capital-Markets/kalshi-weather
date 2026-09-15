"""Tests for CLI clock interpretation and ASOS daily maxima."""

from __future__ import annotations

from datetime import datetime, timezone

from ingestion.climate_time import (
    AsosObservation,
    asos_max_for_climate_day,
    asos_max_in_window,
    cli_max_instant,
    load_cli_time_convention,
    nbm_max_window_utc,
    time_in_window,
)


def test_cli_max_instant_lst() -> None:
    instant = cli_max_instant("2026-07-04", "455 PM", "lst")
    assert instant is not None
    assert instant.hour == 16
    assert instant.minute == 55
    assert instant.tzinfo is not None


def test_cli_max_instant_ldt_differs_in_summer() -> None:
    lst = cli_max_instant("2026-07-04", "455 PM", "lst")
    ldt = cli_max_instant("2026-07-04", "455 PM", "ldt")
    assert lst is not None and ldt is not None
    assert lst.utcoffset() != ldt.utcoffset()


def test_cli_max_instant_unknown_returns_none() -> None:
    assert cli_max_instant("2026-07-04", "455 PM", "unknown") is None


def test_asos_max_picks_highest_tmpf_earliest_on_tie() -> None:
    # LST day 2026-07-04 runs 2026-07-04 05:00Z to 2026-07-05 05:00Z.
    obs = [
        AsosObservation(datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc), 80.0),
        AsosObservation(datetime(2026, 7, 4, 20, 0, tzinfo=timezone.utc), 85.0),
        AsosObservation(datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc), 85.0),
    ]
    max_f, max_ts = asos_max_for_climate_day(obs, "2026-07-04")
    assert max_f == 85.0
    assert max_ts == obs[1].valid_utc


def test_nbm_max_window_is_twelve_z_to_six_z_next_day() -> None:
    start, end = nbm_max_window_utc("2026-07-04")
    assert start == datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 5, 6, 0, tzinfo=timezone.utc)


def test_time_in_window() -> None:
    start, end = nbm_max_window_utc("2026-07-04")
    inside = datetime(2026, 7, 4, 20, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)
    assert time_in_window(inside, start, end)
    assert not time_in_window(outside, start, end)


def test_asos_max_in_nbm_window_excludes_pre_12z() -> None:
    start, end = nbm_max_window_utc("2026-07-04")
    obs = [
        AsosObservation(datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc), 90.0),
        AsosObservation(datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc), 84.0),
        AsosObservation(datetime(2026, 7, 5, 4, 0, tzinfo=timezone.utc), 82.0),
    ]
    max_f, when = asos_max_in_window(obs, start, end)
    assert max_f == 84.0
    assert when == datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)


def test_load_cli_time_convention_defaults_unknown() -> None:
    assert load_cli_time_convention({}) == "unknown"
    assert load_cli_time_convention({"climate": {"cli_time_convention": "lst"}}) == "lst"
