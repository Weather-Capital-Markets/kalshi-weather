"""Tests for forward quote-emission validation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from analysis.spread_census import candle_fields
from analysis.validate_emission_forward import compare_ticker, load_logger_books
from ingestion.client import RequestResult
from ingestion.writer import RawJsonlWriter


def test_compare_ticker_flags_silent_book_change_without_candle(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    ticker = "KXHIGHNY-26AUG12-T90"
    t0 = datetime(2026, 8, 12, 14, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 12, 14, 1, 30, tzinfo=timezone.utc)
    for ts, bid, ask in (
        (t0, 0.40, 0.45),
        (t1, 0.41, 0.44),
    ):
        writer.write(
            ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            endpoint=f"/markets/{ticker}/orderbook",
            category="orderbook",
            key=ticker,
            http_status=200,
            latency_ms=1,
            payload={
                "orderbook_fp": {
                    "yes_dollars": [[f"{bid:.4f}", "10.00"]],
                    "no_dollars": [[f"{1 - ask:.4f}", "10.00"]],
                }
            },
        )
    writer.close()

    books = load_logger_books(
        raw_dir,
        start=datetime(2026, 8, 12).date(),
        end=datetime(2026, 8, 12).date(),
    )
    boundary = int(t0.timestamp())
    boundary -= boundary % 60
    candles = [
        candle_fields(
            {
                "end_period_ts": boundary,
                "yes_bid": {"close_dollars": "0.4000"},
                "yes_ask": {"close_dollars": "0.4500"},
                "volume_fp": "0.00",
            }
        )
    ]
    result = compare_ticker(
        ticker=ticker,
        logger_rows=books[ticker],
        candles=candles,
        boundaries=[boundary],
    )
    assert result["matched"] == 1
    assert len(result["silent_changes"]) == 1


def test_forward_validator_reports_shortfall_with_two_markets(tmp_path: Path, capsys) -> None:
    from analysis import validate_emission_forward as module

    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    tickers = ["KXHIGHNY-26AUG10-T90", "KXHIGHNY-26AUG11-T90"]
    for ticker in tickers:
        writer.write(
            ts_utc="2026-08-10T14:00:00.000Z",
            endpoint=f"/markets/{ticker}/orderbook",
            category="orderbook",
            key=ticker,
            http_status=200,
            latency_ms=1,
            payload={
                "orderbook_fp": {
                    "yes_dollars": [["0.4000", "10.00"]],
                    "no_dollars": [["0.6000", "10.00"]],
                }
            },
        )
    writer.close()

    client = MagicMock()
    client.path.return_value = "/candles"
    client.get.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body={"candlesticks": []},
        error_text=None,
        endpoint="/candles",
    )
    assert (
        module.run(
            config={"api": {"base_url": "https://example.test", "max_requests_per_sec": 100}},
            data_dir=raw_dir,
            start=datetime(2026, 8, 10).date(),
            end=datetime(2026, 8, 10).date(),
            markets=tickers,
            client=client,
            out_dir=tmp_path / "out",
        )
        == 2
    )
    out = capsys.readouterr().out
    assert "SHORTFALL" in out
    assert (tmp_path / "out" / "emission_forward.txt").exists()


def test_forward_validator_run_uses_mock_client(tmp_path: Path, monkeypatch) -> None:
    from analysis import validate_emission_forward as module

    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    tickers = [
        "KXHIGHNY-26AUG10-T90",
        "KXHIGHNY-26AUG11-T90",
        "KXHIGHNY-26AUG12-T90",
    ]
    for ticker in tickers:
        writer.write(
            ts_utc="2026-08-10T14:00:00.000Z",
            endpoint=f"/markets/{ticker}/orderbook",
            category="orderbook",
            key=ticker,
            http_status=200,
            latency_ms=1,
            payload={
                "orderbook_fp": {
                    "yes_dollars": [["0.4000", "10.00"]],
                    "no_dollars": [["0.6000", "10.00"]],
                }
            },
        )
    for extra_day in ("2026-08-11", "2026-08-12"):
        for ticker in tickers:
            writer.write(
                ts_utc=f"{extra_day}T14:00:00.000Z",
                endpoint=f"/markets/{ticker}/orderbook",
                category="orderbook",
                key=ticker,
                http_status=200,
                latency_ms=1,
                payload={
                    "orderbook_fp": {
                        "yes_dollars": [["0.4000", "10.00"]],
                        "no_dollars": [["0.6000", "10.00"]],
                    }
                },
            )
    writer.close()

    client = MagicMock()
    boundary = int(datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc).timestamp())
    boundary -= boundary % 60
    client.path.return_value = "/series/KXHIGHNY/markets/TICKER/candlesticks"
    client.get.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body={
            "candlesticks": [
                {
                    "end_period_ts": boundary,
                    "yes_bid": {"close_dollars": "0.4000"},
                    "yes_ask": {"close_dollars": "0.4000"},
                    "volume_fp": "0.00",
                }
            ]
        },
        error_text=None,
        endpoint="/candlesticks",
    )

    assert (
        module.run(
            config={"api": {"base_url": "https://example.test", "max_requests_per_sec": 100}},
            data_dir=raw_dir,
            start=datetime(2026, 8, 10).date(),
            end=datetime(2026, 8, 12).date(),
            markets=tickers,
            client=client,
        )
        == 0
    )
