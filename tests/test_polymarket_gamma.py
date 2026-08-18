"""Tests for Polymarket Gamma discovery helpers."""

from __future__ import annotations

from datetime import date

from ingestion.polymarket_gamma import (
    bracket_labels,
    enrich_market,
    event_slug_candidates,
    ladder_strike_set,
    open_threshold_markets,
    parse_strike_direction,
)


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


def test_parse_strike_direction_cumulative_ge() -> None:
    assert parse_strike_direction({"groupItemTitle": "94°F or higher"}) == (94, ">=")
    assert parse_strike_direction({"groupItemTitle": "76-77°F"}) == (76, ">=")
    assert parse_strike_direction({"question": "between 80-81°F on August 18"}) == (80, ">=")
    assert parse_strike_direction({"groupItemTitle": "75°F or below"}) == (75, "<=")


def test_open_threshold_markets_adds_pm_meta() -> None:
    event = {
        "slug": "highest-temperature-in-nyc-on-august-18-2026",
        "markets": [
            {
                "slug": "highest-temperature-in-nyc-on-august-18-2026-76-77f",
                "groupItemTitle": "76-77°F",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "clobTokenIds": "[\"123\", \"456\"]",
            },
            {
                "slug": "closed-market",
                "groupItemTitle": "80-81°F",
                "active": True,
                "closed": True,
                "acceptingOrders": False,
            },
        ],
    }
    markets = open_threshold_markets(event)
    assert len(markets) == 1
    assert markets[0]["pm_meta"]["strike_f"] == 76
    assert markets[0]["pm_meta"]["direction"] == ">="
    assert markets[0]["pm_meta"]["yes_token_id"] == "123"


def test_ladder_strike_set_sorted() -> None:
    markets = [
        enrich_market(
            {
                "slug": "a",
                "groupItemTitle": "88-89°F",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
            },
            event_slug="event",
        ),
        enrich_market(
            {
                "slug": "b",
                "groupItemTitle": "76-77°F",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
            },
            event_slug="event",
        ),
    ]
    ladder = ladder_strike_set(markets)
    assert ladder[0]["strike_f"] == 76
    assert ladder[1]["strike_f"] == 88
