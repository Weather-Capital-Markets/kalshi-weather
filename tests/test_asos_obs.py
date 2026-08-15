"""Tests for ASOS backfill ingestion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ingestion.asos_obs import AsosObsBackfill
from ingestion.client import RequestResult
from ingestion.heartbeat import connect
from ingestion.state import asos_month_complete, init_backfill_schema, init_state_schema
from ingestion.writer import read_jsonl_gz


def test_asos_fetch_month_uses_timezone_aware_iem_timestamps(tmp_path: Path) -> None:
    config = {
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "backfill_db": str(tmp_path / "backfill.sqlite"),
        },
        "api": {
            "timeout_sec": 5,
            "max_requests_per_sec": 1,
            "paths": {"markets": "/markets"},
        },
        "asos_obs": {
            "start_date": "2025-05-01",
            "station": "NYC",
            "network": "NY_ASOS",
        },
    }
    captured: dict = {}
    backfill = AsosObsBackfill(config)

    class RecordingClient:
        def close(self) -> None:
            return None

        def get(self, path: str, *, params=None) -> RequestResult:
            captured.update(params or {})
            return RequestResult(
                status_code=200,
                latency_ms=1,
                json_body=None,
                error_text=None,
                endpoint=path,
                text_body="station,valid,tmpf\n",
            )

    try:
        backfill.client = RecordingClient()  # type: ignore[assignment]
        backfill._fetch_month("2025-05", can_complete=False)
    finally:
        backfill.close()

    assert captured["sts"] == "2025-05-01T00:00:00Z"
    assert captured["ets"] == "2025-05-31T23:59:59Z"


def test_asos_backfill_writes_raw_and_marks_month_complete(tmp_path: Path) -> None:
    config = {
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "backfill_db": str(tmp_path / "backfill.sqlite"),
        },
        "api": {
            "timeout_sec": 5,
            "max_requests_per_sec": 1,
            "paths": {"markets": "/markets"},
        },
        "asos_obs": {
            "start_date": "2025-05-01",
            "station": "NYC",
            "network": "NY_ASOS",
        },
    }
    fixture = Path(__file__).resolve().parent / "fixtures" / "asos_nyc_sample.csv"
    csv_body = fixture.read_text(encoding="utf-8")
    backfill = AsosObsBackfill(config)
    backfill.client = MagicMock()
    backfill.client.get.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=None,
        error_text=None,
        endpoint="/cgi-bin/request/asos.py",
        text_body=csv_body,
    )
    backfill._fetch_month("2025-05", can_complete=True)
    backfill.close()

    conn = connect(config["storage"]["backfill_db"])
    init_backfill_schema(conn)
    init_state_schema(conn)
    assert asos_month_complete(conn, "2025-05")
    conn.close()

    files = list((tmp_path / "raw").rglob("asos_obs/*.jsonl.gz"))
    assert len(files) == 1
    records = read_jsonl_gz(files[0])
    assert records[0]["payload"]["text"].startswith("station,valid,tmpf")
