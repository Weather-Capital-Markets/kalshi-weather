"""Integration-style tests for kalshi_logger with mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ingestion.client import RequestResult
from ingestion.kalshi_logger import KalshiLogger
from ingestion.state import get_state, trade_cursor_key
from ingestion.writer import read_jsonl_gz

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class MockKalshiClient:
    def __init__(self, config: dict[str, Any], auth=None, **_kwargs) -> None:
        self.config = config
        self.orderbook_depth = 0
        self.trades_page_limit = 1000

    def close(self) -> None:
        return None

    def path(self, name: str, **kwargs: str) -> str:
        templates = self.config["api"]["paths"]
        return templates[name].format(**kwargs)

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> RequestResult:
        if path == "/markets":
            return RequestResult(
                status_code=200,
                latency_ms=25,
                json_body=_load("markets_open.json"),
                error_text=None,
                endpoint=path,
            )
        if path.startswith("/markets/") and path.endswith("/orderbook"):
            return RequestResult(
                status_code=200,
                latency_ms=10,
                json_body=_load("orderbook.json"),
                error_text=None,
                endpoint=path,
            )
        if path == "/markets/trades":
            return RequestResult(
                status_code=200,
                latency_ms=30,
                json_body=_load("trades_page.json"),
                error_text=None,
                endpoint=path,
            )
        return RequestResult(
            status_code=404,
            latency_ms=5,
            json_body=None,
            error_text="not found",
            endpoint=path,
        )


@pytest.fixture
def config(tmp_path: Path) -> dict[str, Any]:
    return {
        "api": {
            "base_url": "https://example.test/trade-api/v2",
            "paths": {
                "markets": "/markets",
                "market": "/markets/{ticker}",
                "orderbook": "/markets/{ticker}/orderbook",
                "trades": "/markets/trades",
            },
            "timeout_sec": 5,
            "max_requests_per_sec": 100,
            "orderbook_depth": 0,
            "max_retries": 1,
            "trades_page_limit": 1000,
        },
        "series": ["KXHIGHNY"],
        "cadence_sec": {
            "markets": 300,
            "orderbook": 5,
            "trades": 60,
            "settlement": 86400,
        },
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "heartbeat_db": str(tmp_path / "heartbeat.sqlite"),
        },
        "logging": {"level": "WARNING"},
    }


def test_run_once_writes_markets_orderbook_and_heartbeat(
    config: dict[str, Any], tmp_path: Path
) -> None:
    with patch("ingestion.kalshi_logger.KalshiClient", MockKalshiClient):
        app = KalshiLogger(config)
        try:
            app.run_once()
            heartbeat_count = app.conn.execute(
                "SELECT COUNT(*) AS c FROM poll_attempts"
            ).fetchone()["c"]
            remaining_cursors = {
                ticker: get_state(app.conn, trade_cursor_key(ticker))
                for ticker in app._active_tickers
            }
        finally:
            app.close()

    markets_files = list((tmp_path / "raw").rglob("markets/KXHIGHNY.jsonl.gz"))
    orderbook_files = list((tmp_path / "raw").rglob("orderbook/*.jsonl.gz"))
    assert len(markets_files) >= 1
    assert len(orderbook_files) >= 2
    assert heartbeat_count >= 4
    assert all(cursor is None for cursor in remaining_cursors.values())

    markets_records = read_jsonl_gz(markets_files[0])
    assert markets_records[0]["payload"]["markets"]


def test_trade_cursor_advances_after_raw_write_before_seen_commit(
    config: dict[str, Any],
) -> None:
    ticker = "KXHIGHNY-26AUG14-T90"
    with (
        patch("ingestion.kalshi_logger.KalshiClient", MockKalshiClient),
        patch(
            "ingestion.kalshi_logger.mark_trades_seen",
            side_effect=RuntimeError("simulated crash"),
        ),
    ):
        app = KalshiLogger(config)
        try:
            with pytest.raises(RuntimeError, match="simulated crash"):
                app._poll_trades_for_market("KXHIGHNY", ticker)
            assert get_state(app.conn, trade_cursor_key(ticker)) == "next-page-cursor"
            trade_files = list(Path(config["storage"]["raw_dir"]).rglob("trades/*.jsonl.gz"))
            assert len(read_jsonl_gz(trade_files[0])) == 1
        finally:
            app.close()
