"""Tests for venue convention classification and changeover detection."""

from __future__ import annotations

import pandas as pd

from analysis.venue_eras import (
    BOTH_AGREE,
    CIVIL_ET,
    LST_FIXED,
    UNSPECIFIED,
    change_points,
    close_convention,
    close_offset_minutes,
    daily_modal_values,
    is_dst_transition_day,
    settlement_phrase,
)

MODERN_CONDITION = (
    "The Last Trading Time will be 11:59 PM ET on August 12, 2026 regardless of any "
    "data releases or events occurring. Expiration will occur on the sooner of the "
    "first 7:00 or 8:00\nAM ET following the release of the data for August 12, 2026, "
    "or one week after August 12, 2026."
)
LEGACY_CONDITION = (
    "The Last Trading Time will be 11:59 PM ET on September 03, 2024 regardless of any "
    "data releases or events occurring. Expiration will occur on the sooner of the "
    "first 10:00 AM following the release of the data for September 03, 2024, or one "
    "week after September 03, 2024."
)


def test_offset_from_climate_day_end_follows_daylight_saving() -> None:
    # Same 11:59 PM civil-ET close lands 61 minutes before the LST day end under
    # EDT and 1 minute before it under EST. That swing is not a venue change.
    summer = close_offset_minutes(
        {"ticker": "HIGHNY-24AUG15-T83", "close_time": "2024-08-16T03:59:00Z"}
    )
    winter = close_offset_minutes(
        {"ticker": "HIGHNY-24JAN15-T40", "close_time": "2024-01-16T04:59:00Z"}
    )
    assert summer == 61
    assert winter == 1


def test_close_convention_only_identifies_under_edt() -> None:
    assert close_convention({"close_time": "2024-08-16T03:59:00Z"}) == CIVIL_ET
    assert close_convention({"close_time": "2026-06-13T04:59:00Z"}) == LST_FIXED
    # Under EST both rules name the same instant, so the day proves nothing.
    assert close_convention({"close_time": "2026-01-10T04:59:00Z"}) == BOTH_AGREE


def test_daylight_saving_transition_days_are_identified() -> None:
    assert is_dst_transition_day("2026-03-08")
    assert is_dst_transition_day("2025-11-02")
    assert not is_dst_transition_day("2026-06-12")


def test_settlement_phrase_reads_both_wordings_and_the_legacy_reference() -> None:
    assert settlement_phrase({"early_close_condition": MODERN_CONDITION}) == "7:00 OR 8:00 AM"
    # The legacy wording omits "ET"; requiring it would hide the whole 10 AM era.
    assert settlement_phrase({"rules_secondary": LEGACY_CONDITION}) == "10:00 AM"
    assert (
        settlement_phrase({"rules_secondary": "Please see Rule 100.19 in the Rulebook."})
        == UNSPECIFIED
    )


def test_change_points_report_the_boundary_dates() -> None:
    daily = pd.Series(
        ["a", "a", "b", "b"],
        index=["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19"],
    )
    points = change_points(daily)
    assert len(points) == 1
    assert points[0]["last_date_before"] == "2026-03-17"
    assert points[0]["first_date_after"] == "2026-03-18"
    assert points[0]["value_before"] == "a"
    assert points[0]["value_after"] == "b"


def test_daily_modal_value_survives_one_odd_market() -> None:
    rows = pd.DataFrame(
        {
            "climate_date": ["2026-06-12"] * 3,
            "close_offset_min": [1, 1, 721],
        }
    )
    assert daily_modal_values(rows, "close_offset_min").loc["2026-06-12"] == 1
