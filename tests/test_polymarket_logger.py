"""Tests for Polymarket logger isolation and probe."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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


def test_polymarket_probe_prints_raw_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
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
            json_body={"marketData": {"bids": [{"px": {"value": "0.50"}, "qty": "10"}]}},
            error_text=None,
            endpoint="/v1/markets/nyc-high-temp-example/book",
        ),
    ]
    try:
        assert app.probe() == 0
    finally:
        app.close()
    out = capsys.readouterr().out
    assert "PROBE GET" in out
    assert "nyc-high-temp-example" in out
