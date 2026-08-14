"""Tests for heartbeat SQLite logging."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.heartbeat import (
    connect,
    format_status_summary,
    last_hour_summary,
    record_attempt,
)


def _recent_ts(offset_sec: int = 0) -> str:
    dt = datetime.now(timezone.utc)
    if offset_sec:
        from datetime import timedelta

        dt = dt + timedelta(seconds=offset_sec)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.fixture
def conn(tmp_path: Path):
    db = connect(tmp_path / "heartbeat.sqlite")
    yield db
    db.close()


def test_every_attempt_creates_row(conn) -> None:
    record_attempt(
        conn,
        ts_utc=_recent_ts(),
        endpoint="/markets",
        ticker="KXHIGHNY",
        ok=True,
        http_status=200,
        latency_ms=10,
        error_text=None,
    )
    record_attempt(
        conn,
        ts_utc=_recent_ts(5),
        endpoint="/markets/KXHIGHNY-T90/orderbook",
        ticker="KXHIGHNY-T90",
        ok=False,
        http_status=500,
        latency_ms=20,
        error_text="server error",
    )
    count = conn.execute("SELECT COUNT(*) AS c FROM poll_attempts").fetchone()["c"]
    assert count == 2


def test_last_hour_summary_counts(conn) -> None:
    record_attempt(
        conn,
        ts_utc=_recent_ts(),
        endpoint="/markets",
        ticker="KXHIGHNY",
        ok=True,
        http_status=200,
        latency_ms=10,
        error_text=None,
    )
    record_attempt(
        conn,
        ts_utc=_recent_ts(5),
        endpoint="/markets",
        ticker="KXHIGHNY",
        ok=False,
        http_status=429,
        latency_ms=15,
        error_text="rate limited",
    )
    rows = last_hour_summary(conn)
    assert len(rows) >= 1
    markets_row = next(r for r in rows if r.endpoint == "/markets")
    assert markets_row.attempts == 2
    assert markets_row.failures == 1
    summary = format_status_summary(rows)
    assert "/markets" in summary
