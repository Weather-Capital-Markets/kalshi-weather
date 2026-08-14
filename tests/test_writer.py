"""Tests for RawJsonlWriter."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.writer import RawJsonlWriter, read_jsonl_gz, utc_date_str


@pytest.fixture
def writer(tmp_path: Path) -> RawJsonlWriter:
    return RawJsonlWriter(tmp_path / "raw")


def test_write_and_read_roundtrip(writer: RawJsonlWriter) -> None:
    ts = "2026-08-14T10:00:00.000Z"
    path = writer.write(
        ts_utc=ts,
        endpoint="/markets/KXHIGHNY-T90/orderbook",
        category="orderbook",
        key="KXHIGHNY-T90",
        http_status=200,
        latency_ms=12,
        payload={"orderbook_fp": {"yes_dollars": []}},
    )
    writer.close()

    records = read_jsonl_gz(path)
    assert len(records) == 1
    assert records[0]["ts_utc"] == ts
    assert records[0]["http_status"] == 200
    assert records[0]["latency_ms"] == 12
    assert "orderbook_fp" in records[0]["payload"]


def test_daily_rotation(writer: RawJsonlWriter) -> None:
    writer.write(
        ts_utc="2026-08-14T23:59:00.000Z",
        endpoint="/markets",
        category="markets",
        key="KXHIGHNY",
        http_status=200,
        latency_ms=1,
        payload={"day": 14},
    )
    writer.write(
        ts_utc="2026-08-15T00:01:00.000Z",
        endpoint="/markets",
        category="markets",
        key="KXHIGHNY",
        http_status=200,
        latency_ms=1,
        payload={"day": 15},
    )
    writer.close()

    path_14 = writer._file_path("2026-08-14", "markets", "KXHIGHNY")
    path_15 = writer._file_path("2026-08-15", "markets", "KXHIGHNY")
    assert len(read_jsonl_gz(path_14)) == 1
    assert len(read_jsonl_gz(path_15)) == 1


def test_restart_repairs_incomplete_final_member(writer: RawJsonlWriter) -> None:
    ts = "2026-08-14T10:00:00.000Z"
    path = writer._file_path(utc_date_str(ts), "orderbook", "TICKER")

    writer.write(
        ts_utc=ts,
        endpoint="/markets/TICKER/orderbook",
        category="orderbook",
        key="TICKER",
        http_status=200,
        latency_ms=5,
        payload={"n": 1},
    )

    complete_size = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"\x1f\x8b\x08\x00truncated")

    restarted = RawJsonlWriter(writer.raw_dir)
    restarted.write(
        ts_utc=ts,
        endpoint="/markets/TICKER/orderbook",
        category="orderbook",
        key="TICKER",
        http_status=200,
        latency_ms=6,
        payload={"n": 2},
    )

    assert path.stat().st_size > complete_size
    assert [record["payload"]["n"] for record in read_jsonl_gz(path)] == [1, 2]
