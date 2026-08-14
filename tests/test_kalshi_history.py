"""History backfill resume and dry-run tests (mocked HTTP)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ingestion.client import RequestResult
from ingestion.kalshi_history import HistoryBackfill
from ingestion.state import get_candle_progress, init_backfill_schema, set_candle_progress
from ingestion.writer import utc_now_iso


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "api": {
            "base_url": "https://example.test/trade-api/v2",
            "paths": {
                "markets": "/markets",
                "market": "/markets/{ticker}",
                "historical_cutoff": "/historical/cutoff",
                "historical_markets": "/historical/markets",
                "candlesticks": "/series/{series_ticker}/markets/{ticker}/candlesticks",
                "historical_candlesticks": "/historical/markets/{ticker}/candlesticks",
            },
            "timeout_sec": 5,
            "max_requests_per_sec": 100,
            "max_retries": 0,
        },
        "series": ["KXHIGHNY"],
        "history": {
            "series": ["KXHIGHNY"],
            "statuses": ["settled"],
            "candle_period_minutes": 1,
            "candle_chunk_minutes": 60,
        },
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "backfill_db": str(tmp_path / "backfill.sqlite"),
        },
        "logging": {"level": "WARNING"},
    }


class ScriptedClient:
    def __init__(self, config: dict[str, Any], **_kwargs) -> None:
        self.config = config
        self.calls: list[str] = []

    def close(self) -> None:
        return None

    def path(self, name: str, **kwargs: str) -> str:
        return self.config["api"]["paths"][name].format(**kwargs)

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> RequestResult:
        self.calls.append(path)
        if path == "/historical/cutoff":
            body: dict[str, Any] = {"market_settled_ts": "2026-05-01T00:00:00Z"}
        elif path == "/markets":
            body = {
                "markets": [
                    {
                        "ticker": "KXHIGHNY-26JUL04-T90",
                        "open_time": "2026-07-02T10:00:00Z",
                        "close_time": "2026-07-05T05:00:00Z",
                        "status": "settled",
                    }
                ],
                "cursor": "",
            }
        elif path == "/historical/markets":
            body = {"markets": [], "cursor": ""}
        elif "candlesticks" in path:
            body = {
                "ticker": "KXHIGHNY-26JUL04-T90",
                "candlesticks": [
                    {
                        "end_period_ts": 1750000000,
                        "yes_bid": {"close_dollars": "0.40"},
                        "yes_ask": {"close_dollars": "0.42"},
                        "volume_fp": "10.00",
                    }
                ],
            }
        else:
            body = {}
        return RequestResult(
            status_code=200,
            latency_ms=1,
            json_body=body,
            error_text=None,
            endpoint=path,
        )


def test_dry_run_does_not_write_candles(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    app = HistoryBackfill(config)
    app.client = ScriptedClient(config)
    try:
        markets = app.enumerate_markets(persist=False)
        app.print_dry_run(markets)
    finally:
        app.close()
    out = capsys.readouterr().out
    assert "markets: 1" in out
    assert "volume_estimate" in out
    assert "contingency" in out
    assert not list((tmp_path / "raw").rglob("candlesticks/*.jsonl.gz"))
    assert not any("candlesticks" in call for call in app.client.calls)


def test_completed_market_is_not_refetched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = HistoryBackfill(config)
    app.client = ScriptedClient(config)
    try:
        init_backfill_schema(app.conn)
        set_candle_progress(
            app.conn,
            ticker="KXHIGHNY-26JUL04-T90",
            last_end_ts=9999999999,
            complete=True,
            updated_utc=utc_now_iso(),
        )
        from ingestion.state import upsert_history_market

        upsert_history_market(
            app.conn,
            ticker="KXHIGHNY-26JUL04-T90",
            series_ticker="KXHIGHNY",
            open_time="2026-07-02T10:00:00Z",
            close_time="2026-07-05T05:00:00Z",
            status="settled",
            enumerated_utc=utc_now_iso(),
        )
        app.backfill_candles()
    finally:
        app.close()
    assert not any("candlesticks" in call for call in app.client.calls)


def test_progress_advances_only_after_raw_write(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = HistoryBackfill(config)
    client = ScriptedClient(config)
    app.client = client
    try:
        from ingestion.state import upsert_history_market

        upsert_history_market(
            app.conn,
            ticker="KXHIGHNY-26JUL04-T90",
            series_ticker="KXHIGHNY",
            open_time="2026-07-04T00:00:00Z",
            close_time="2026-07-04T02:00:00Z",
            status="settled",
            enumerated_utc=utc_now_iso(),
        )

        original_write = app.writer.write

        def boom(**kwargs):
            raise RuntimeError("simulated crash")

        app.writer.write = boom  # type: ignore[method-assign]
        try:
            app.backfill_candles()
        except RuntimeError:
            pass
        progress = get_candle_progress(app.conn, "KXHIGHNY-26JUL04-T90")
        assert progress is None
        app.writer.write = original_write  # type: ignore[method-assign]
    finally:
        app.close()
