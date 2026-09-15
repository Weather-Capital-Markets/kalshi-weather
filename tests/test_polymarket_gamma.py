"""Tests for Polymarket Gamma discovery helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from ingestion.client import RequestResult
from ingestion.polymarket_gamma import (
    bracket_labels,
    bracket_ladder_set,
    discover_daily_events,
    enrich_market,
    event_slug_candidates,
    open_bracket_markets,
    parse_bracket,
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


def test_parse_bracket_disjoint_ranges() -> None:
    assert parse_bracket({"groupItemTitle": "94°F or higher"}) == {
        "bracket_kind": "tail_above",
        "bracket_low_f": 94,
        "bracket_high_f": None,
        "bracket_label": "94°F or higher",
    }
    assert parse_bracket({"groupItemTitle": "76-77°F"}) == {
        "bracket_kind": "between",
        "bracket_low_f": 76,
        "bracket_high_f": 77,
        "bracket_label": "76-77°F",
    }
    assert parse_bracket({"question": "between 80-81°F on August 18"}) == {
        "bracket_kind": "between",
        "bracket_low_f": 80,
        "bracket_high_f": 81,
        "bracket_label": "80-81°F",
    }
    assert parse_bracket({"groupItemTitle": "75°F or below"}) == {
        "bracket_kind": "tail_below",
        "bracket_low_f": None,
        "bracket_high_f": 75,
        "bracket_label": "75°F or below",
    }


def test_open_bracket_markets_adds_pm_meta() -> None:
    event = {
        "slug": "highest-temperature-in-nyc-on-august-18-2026",
        "markets": [
            {
                "slug": "highest-temperature-in-nyc-on-august-18-2026-76-77f",
                "groupItemTitle": "76-77°F",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "negRisk": True,
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
    markets = open_bracket_markets(event)
    assert len(markets) == 1
    assert markets[0]["pm_meta"]["bracket_kind"] == "between"
    assert markets[0]["pm_meta"]["bracket_low_f"] == 76
    assert markets[0]["pm_meta"]["bracket_high_f"] == 77
    assert markets[0]["pm_meta"]["neg_risk"] is True
    assert markets[0]["pm_meta"]["yes_token_id"] == "123"


def test_bracket_ladder_set_sorted() -> None:
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
    ladder = bracket_ladder_set(markets)
    assert ladder[0]["bracket_low_f"] == 76
    assert ladder[1]["bracket_low_f"] == 88


def test_discover_daily_events_returns_events_and_results() -> None:
    client = MagicMock()
    event = {
        "slug": "highest-temperature-in-nyc-on-august-18-2026",
        "markets": [
            {
                "slug": "m1",
                "groupItemTitle": "76-77°F",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "clobTokenIds": "[\"1\"]",
            }
        ],
    }
    client.list_series_events.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=[],
        error_text=None,
        endpoint="/events",
    )
    client.event_by_slug.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=event,
        error_text=None,
        endpoint="/events/slug/test",
    )
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    discovered, results = discover_daily_events(
        client,
        series_slug="nyc-daily-weather",
        event_prefix="highest-temperature-in-nyc-on",
        horizon_days=1,
        discovery_limit=5,
        now=now,
    )
    assert len(discovered) == 1
    assert discovered[0][0] == "highest-temperature-in-nyc-on-august-18-2026"
    assert len(results) >= 2
