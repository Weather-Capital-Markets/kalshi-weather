"""Tests for Polymarket logger isolation and probe."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion.client import RequestResult
from ingestion.heartbeat import connect, init_schema
from ingestion.polymarket_logger import PolymarketLogger
from ingestion.writer import read_jsonl_gz


def _pm_config(tmp_path: Path) -> dict:
    return {
        "api": {
            "base_url": "https://gateway.polymarket.us",
            "max_requests_per_sec": 100,
            "timeout_sec": 5,
            "max_retries": 1,
        },
        "gamma": {
            "base_url": "https://gamma-api.polymarket.com",
            "max_requests_per_sec": 100,
            "timeout_sec": 5,
            "max_retries": 1,
            "series_slug": "nyc-daily-weather",
            "event_slug_prefix": "highest-temperature-in-nyc-on",
            "discovery_limit": 5,
        },
        "clob": {"base_url": "https://clob.polymarket.com"},
        "markets": {
            "list_path": "/v1/markets",
            "book_path_template": "/v1/markets/{slug}/book",
            "slugs": ["nyc-high-temp-example"],
            "limit": 10,
        },
        "cadence_sec": {"markets": 300, "orderbook": 5},
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "heartbeat_db": str(tmp_path / "polymarket_heartbeat.sqlite"),
        },
    }


def _sample_event(slug: str, day: date) -> dict:
    market_slug = f"{slug}-75forbelow"
    return {
        "id": "1",
        "slug": slug,
        "title": f"Highest temperature in NYC on {day.strftime('%B')} {day.day}?",
        "seriesSlug": "nyc-daily-weather",
        "markets": [
            {
                "id": "99",
                "slug": market_slug,
                "question": "Will the highest temperature in New York City be 75°F or below?",
                "groupItemTitle": "75°F or below",
                "groupItemThreshold": "0",
                "negRisk": True,
                "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
                "clobTokenIds": "[\"69086838067399619222212138602436022414711389191258954262625233286462338437735\"]",
            },
            {
                "id": "100",
                "slug": f"{slug}-76-77f",
                "question": "Will the highest temperature in New York City be between 76-77°F?",
                "groupItemTitle": "76-77°F",
                "groupItemThreshold": "1",
                "negRisk": True,
            },
        ],
    }


def test_polymarket_once_writes_isolated_raw_and_heartbeat(tmp_path: Path) -> None:
    config = _pm_config(tmp_path)
    app = PolymarketLogger(config)
    app.client = MagicMock()
    app.client.get.side_effect = [
        RequestResult(
            status_code=200,
            latency_ms=1,
            json_body={"markets": [{"slug": "nyc-high-temp-example"}]},
            error_text=None,
            endpoint="/v1/markets",
        ),
        RequestResult(
            status_code=200,
            latency_ms=1,
            json_body={"marketData": {"bids": [], "offers": []}},
            error_text=None,
            endpoint="/v1/markets/nyc-high-temp-example/book",
        ),
    ]
    try:
        app.run_once()
    finally:
        app.close()

    pm_files = list((tmp_path / "raw").rglob("pm_orderbook/*.jsonl.gz"))
    assert len(pm_files) == 1
    records = read_jsonl_gz(pm_files[0])
    assert "marketData" in records[0]["payload"] or records[0]["payload"]

    conn = connect(config["storage"]["heartbeat_db"])
    init_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM poll_attempts").fetchone()[0]
    conn.close()
    assert count >= 2


def test_polymarket_probe_prints_gamma_and_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _pm_config(tmp_path)
    app = PolymarketLogger(config)
    app.client = MagicMock()

    today = date(2026, 8, 18)
    tomorrow = today.replace(day=19)
    current_slug = "highest-temperature-in-nyc-on-august-18-2026"
    next_slug = "highest-temperature-in-nyc-on-august-19-2026"
    current_event = _sample_event(current_slug, today)
    next_event = _sample_event(next_slug, tomorrow)

    gamma = MagicMock()
    gamma.list_series_events.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=[current_event, next_event],
        error_text=None,
        endpoint="/events",
    )
    gamma.event_by_slug.side_effect = lambda slug: RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=current_event if "august-18" in slug else next_event,
        error_text=None,
        endpoint=f"/events/slug/{slug}",
    )
    gamma.close = MagicMock()

    app.client.get.side_effect = [
        RequestResult(
            status_code=200,
            latency_ms=1,
            json_body={"markets": [{"slug": current_event["markets"][0]["slug"]}]},
            error_text=None,
            endpoint="/v1/markets",
        ),
        RequestResult(
            status_code=404,
            latency_ms=1,
            json_body={"code": 5, "message": "not found"},
            error_text="not found",
            endpoint=f"/v1/markets/{current_event['markets'][0]['slug']}/book",
        ),
    ]

    clob_response = MagicMock()
    clob_response.status_code = 200
    clob_response.json.return_value = {"bids": [{"price": "0.001", "size": "10"}], "asks": []}

    with (
        patch("ingestion.polymarket_logger.PolymarketGammaClient", return_value=gamma),
        patch("ingestion.polymarket_logger.datetime") as mock_dt,
        patch("httpx.get", return_value=clob_response),
    ):
        mock_dt.now.return_value = __import__("datetime").datetime(
            2026, 8, 18, 2, 0, 0, tzinfo=__import__("datetime").timezone.utc
        )
        mock_dt.side_effect = lambda *args, **kwargs: __import__("datetime").datetime(*args, **kwargs)
        try:
            assert app.probe() == 0
        finally:
            app.close()

    out = capsys.readouterr().out
    assert "PROBE Gamma GET /events" in out
    assert "EXHAUSTIVE_BRACKET_LADDER" in out
    assert "RAW MARKETS current event" in out
    assert current_slug in out
    assert next_slug in out
    assert "PROBE CLOB GET /book" in out
    assert "PROBE gateway GET markets list" in out
