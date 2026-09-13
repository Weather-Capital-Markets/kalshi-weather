"""Stage 0: 12-way emission convention sweep."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from analysis.emission_bucketing import bucket_end_period_ts, iter_conventions
from analysis.emission_compare import compare_ticker_convention
from analysis.emission_convention_sweep import sweep_conventions
from analysis.spread_census import candle_fields
from analysis.validate_emission_forward import EmissionValidationError, fetch_candles
from ingestion.client import RequestResult
from ingestion.writer import RawJsonlWriter


def test_twelve_conventions_enumerated() -> None:
    assert len(list(iter_conventions())) == 12


def test_interval_end_buckets_silent_change_at_period_end(tmp_path: Path) -> None:
    ticker = "KXHIGHNY-26AUG12-T90"
    t0 = datetime(2026, 8, 12, 14, 0, 0, tzinfo=timezone.utc)
    t_change = datetime(2026, 8, 12, 14, 0, 30, tzinfo=timezone.utc)
    period_end = bucket_end_period_ts(
        t_change,
        interval_anchor="interval_end",
        timezone_mode="UTC",
        bucket_offset=0,
    )
    candles = [
        candle_fields(
            {
                "end_period_ts": period_end,
                "yes_bid": {"close_dollars": "0.4000"},
                "yes_ask": {"close_dollars": "0.4500"},
                "volume_fp": "0.00",
            }
        )
    ]
    rows = [
        (t0, 0.40, 0.45),
        (t_change, 0.41, 0.44),
    ]
    start_result = compare_ticker_convention(
        ticker=ticker,
        logger_rows=rows,
        candles=candles,
        boundaries=[period_end],
        interval_anchor="interval_start",
        timezone_mode="UTC",
        bucket_offset=0,
    )
    end_result = compare_ticker_convention(
        ticker=ticker,
        logger_rows=rows,
        candles=candles,
        boundaries=[period_end],
        interval_anchor="interval_end",
        timezone_mode="UTC",
        bucket_offset=0,
    )
    assert len(start_result["silent_changes"]) == 1
    assert len(end_result["silent_changes"]) == 0


def test_fetch_candles_raises_on_api_failure() -> None:
    client = MagicMock()
    client.path.return_value = "/candles"
    client.get.return_value = RequestResult(
        status_code=500,
        latency_ms=1,
        json_body=None,
        error_text="server error",
        endpoint="/candles",
    )
    with pytest.raises(EmissionValidationError):
        fetch_candles(client, "KXHIGHNY-26AUG12-T90", 0, 100)


def test_sweep_writes_twelve_rows(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    tickers = [
        "KXHIGHNY-26AUG10-T90",
        "KXHIGHNY-26AUG11-T90",
        "KXHIGHNY-26AUG12-T90",
    ]
    for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
        for ticker in tickers:
            writer.write(
                ts_utc=f"{day}T14:00:00.000Z",
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

    boundary = int(datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc).timestamp())
    client = MagicMock()
    client.path.return_value = "/candles"
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
        endpoint="/candles",
    )

    report = sweep_conventions(
        config={"api": {"base_url": "https://example.test", "max_requests_per_sec": 100}},
        data_dir=raw_dir,
        start=datetime(2026, 8, 10).date(),
        end=datetime(2026, 8, 12).date(),
        markets=tickers,
        client=client,
    )
    assert len(report["table"]) == 12
    assert report["sweep_status"] in {"COMPLETE", "INCOMPLETE"}
    assert report["rows_computed"].endswith("/12")
    if report["sweep_status"] == "COMPLETE":
        assert report.get("winner") is not None
        assert "branch" in report
        assert report.get("reconstruction_error_bound")
    else:
        assert "branch" not in report
