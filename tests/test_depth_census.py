"""Tests for logger depth census."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from analysis.depth_census import build_depth_snapshots, load_logger_orderbook_snapshots
from ingestion.writer import RawJsonlWriter


def test_build_depth_snapshots_computes_horizon_metrics(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    ticker = "KXHIGHNY-26AUG12-T90"
    writer.write(
        ts_utc="2026-08-11T12:00:00.000Z",
        endpoint="/markets/KXHIGHNY/orderbook",
        category="orderbook",
        key=ticker,
        http_status=200,
        latency_ms=1,
        payload={
            "orderbook_fp": {
                "yes_dollars": [["0.5000", "100.00"], ["0.4900", "50.00"]],
                "no_dollars": [["0.5200", "80.00"]],
            }
        },
    )
    writer.close()

    start = datetime(2026, 8, 11).date()
    snapshots = load_logger_orderbook_snapshots(raw_dir, start=start, end=start)
    markets = {
        ticker: {
            "ticker": ticker,
            "open_time": "2026-08-10T00:00:00Z",
            "close_time": "2026-08-13T04:59:00Z",
        }
    }
    table = build_depth_snapshots(
        snapshots_by_ticker=snapshots,
        markets=markets,
        horizons_h=(24,),
        primary_band=(0.10, 0.90),
    )
    assert len(table) == 1
    assert table.iloc[0]["depth_within_2c"] > 0
    assert table.iloc[0]["dollar_depth_within_2c"] > 0
