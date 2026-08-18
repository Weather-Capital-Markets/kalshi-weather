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
        "gamma": {
            "base_url": "https://gamma-api.polymarket.com",
            "max_requests_per_sec": 100,
            "timeout_sec": 5,
            "max_retries": 1,
            "series_slug": "nyc-daily-weather",
            "event_slug_prefix": "highest-temperature-in-nyc-on",
            "discovery_limit": 5,
            "horizon_days": 2,
        },
        "clob": {
            "base_url": "https://clob.polymarket.com",
            "book_path": "/book",
            "max_requests_per_sec": 100,
            "timeout_sec": 5,
            "max_retries": 1,
        },
        "markets": {
            "series_slug": "nyc-daily-weather",
            "horizon_days": 2,
        },
        "cadence_sec": {"markets": 300, "orderbook": 5},
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "heartbeat_db": str(tmp_path / "polymarket_heartbeat.sqlite"),
        },
    }


def _threshold_market(slug: str, title: str, token: str = "111") -> dict:
    return {
        "slug": slug,
        "groupItemTitle": title,
        "question": f"Will the highest temperature in New York City be {title}?",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": f'["{token}", "222"]',
    }


def _sample_event(slug: str, day: date) -> dict:
    return {
        "id": "1",
        "slug": slug,
        "title": f"Highest temperature in NYC on {day.strftime('%B')} {day.day}?",
        "seriesSlug": "nyc-daily-weather",
        "markets": [
            _threshold_market(f"{slug}-76-77f", "76-77°F", "token-76"),
            _threshold_market(f"{slug}-78-79f", "78-79°F", "token-78"),
        ],
    }


def test_polymarket_once_writes_all_ladder_books(tmp_path: Path) -> None:
    config = _pm_config(tmp_path)
    app = PolymarketLogger(config)

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

    clob = MagicMock()
    clob.get_book.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body={"bids": [{"price": "0.5", "size": "10"}], "asks": []},
        error_text=None,
        endpoint="/book",
    )
    clob.close = MagicMock()

    app.gamma = gamma
    app.clob = clob

    with patch("ingestion.polymarket_logger.discover_daily_events") as discover:
        discover.return_value = (
            [
                (current_slug, current_event),
                (next_slug, next_event),
            ],
            [],
        )
        try:
            app.run_once()
        finally:
            app.close()

    market_files = list((tmp_path / "raw").rglob("pm_markets/*.jsonl.gz"))
    assert len(market_files) == 2
    book_files = list((tmp_path / "raw").rglob("pm_orderbook/*.jsonl.gz"))
    assert len(book_files) == 4
    sample = read_jsonl_gz(book_files[0])[0]
    assert sample["payload"]["pm_meta"]["direction"] == ">="
    assert "book" in sample["payload"]

    conn = connect(config["storage"]["heartbeat_db"])
    init_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM poll_attempts").fetchone()[0]
    conn.close()
    assert count >= 4


def test_polymarket_probe_prints_strike_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _pm_config(tmp_path)
    app = PolymarketLogger(config)

    today = date(2026, 8, 18)
    current_slug = "highest-temperature-in-nyc-on-august-18-2026"
    current_event = _sample_event(current_slug, today)

    gamma = MagicMock()
    gamma.list_series_events.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=[current_event],
        error_text=None,
        endpoint="/events",
    )
    gamma.event_by_slug.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=current_event,
        error_text=None,
        endpoint=f"/events/slug/{current_slug}",
    )
    gamma.close = MagicMock()

    clob = MagicMock()
    clob.get_book.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body={"bids": [], "asks": [{"price": "0.6", "size": "5"}]},
        error_text=None,
        endpoint="/book",
    )
    clob.close = MagicMock()

    app.gamma = gamma
    app.clob = clob

    with patch("ingestion.polymarket_logger.datetime") as mock_dt:
        mock_dt.now.return_value = __import__("datetime").datetime(
            2026, 8, 18, 2, 0, 0, tzinfo=__import__("datetime").timezone.utc
        )
        mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)
        try:
            assert app.probe() == 0
        finally:
            app.close()

    out = capsys.readouterr().out
    assert "CUMULATIVE_THRESHOLD_BINARIES" in out
    assert "strike_set_ge" in out
    assert "76-77f" in out
    assert "PROBE CLOB GET /book" in out
