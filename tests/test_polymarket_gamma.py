"""Tests for Polymarket Gamma discovery helpers."""

from __future__ import annotations

from datetime import date

from ingestion.polymarket_gamma import bracket_labels, event_slug_candidates


def test_event_slug_candidates_include_year_and_short_form() -> None:
    day = date(2026, 8, 18)
    slugs = event_slug_candidates(day)
    assert slugs == [
        "highest-temperature-in-nyc-on-august-18-2026",
        "highest-temperature-in-nyc-on-august-18",
    ]


def test_bracket_labels_use_group_item_title() -> None:
    markets = [
        {"groupItemTitle": "75°F or below", "question": "q1"},
        {"groupItemTitle": "76-77°F", "question": "q2"},
    ]
    assert bracket_labels(markets) == ["75°F or below", "76-77°F"]
