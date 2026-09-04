"""Tests for ASOS backfill ingestion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ingestion.asos_obs import AsosObsBackfill, resolve_stations
from ingestion.asos_parse import infer_station_from_key, load_asos_observations_from_raw
from ingestion.client import RequestResult
from ingestion.heartbeat import connect
from ingestion.state import (
    asos_month_complete,
    init_backfill_schema,
    init_state_schema,
    set_asos_month_complete,
)
from ingestion.writer import RawJsonlWriter, read_jsonl_gz, utc_now_iso


def test_resolve_stations_prefers_list() -> None:
    assert resolve_stations({"stations": ["NYC", "LGA"]}) == ["NYC", "LGA"]
    assert resolve_stations({"station": "NYC"}) == ["NYC"]
    assert resolve_stations({}) == ["NYC"]


def test_infer_station_from_key() -> None:
    assert infer_station_from_key("NYC_2025-05") == "NYC"
    assert infer_station_from_key("LGA_2021-08") == "LGA"
    assert infer_station_from_key("2025-05") == "NYC"


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
            "stations": ["NYC"],
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
        backfill._fetch_month("NYC", "2025-05", can_complete=False)
    finally:
        backfill.close()

    assert captured["station"] == "NYC"
    assert captured["sts"] == "2025-05-01T00:00:00Z"
    assert captured["ets"] == "2025-05-31T23:59:59Z"


def test_asos_header_only_csv_does_not_mark_complete(tmp_path: Path) -> None:
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
            "stations": ["NYC"],
            "network": "NY_ASOS",
        },
    }
    backfill = AsosObsBackfill(config)
    backfill.client = MagicMock()
    backfill.client.get.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=None,
        error_text=None,
        endpoint="/cgi-bin/request/asos.py",
        text_body="station,valid,tmpf\n",
    )
    backfill._fetch_month("NYC", "2025-05", can_complete=True)
    backfill.close()

    conn = connect(config["storage"]["backfill_db"])
    init_backfill_schema(conn)
    init_state_schema(conn)
    assert not asos_month_complete(conn, "NYC", "2025-05")
    conn.close()


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
            "stations": ["NYC"],
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
    backfill._fetch_month("NYC", "2025-05", can_complete=True)
    backfill.close()

    conn = connect(config["storage"]["backfill_db"])
    init_backfill_schema(conn)
    init_state_schema(conn)
    assert asos_month_complete(conn, "NYC", "2025-05")
    conn.close()

    files = list((tmp_path / "raw").rglob("asos_obs/*.jsonl.gz"))
    assert len(files) == 1
    assert files[0].name == "NYC_2025-05.jsonl.gz"
    records = read_jsonl_gz(files[0])
    assert records[0]["payload"]["text"].startswith("station,valid,tmpf")
    assert records[0]["payload"]["station"] == "NYC"


def test_asos_multi_station_writes_separate_keys(tmp_path: Path) -> None:
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
            "stations": ["NYC", "LGA"],
            "network": "NY_ASOS",
        },
    }
    backfill = AsosObsBackfill(config)
    backfill.client = MagicMock()
    backfill.client.get.return_value = RequestResult(
        status_code=200,
        latency_ms=1,
        json_body=None,
        error_text=None,
        endpoint="/cgi-bin/request/asos.py",
        text_body="station,valid,tmpf\nNYC,2025-05-01 12:00,70.0\n",
    )
    backfill._fetch_month("NYC", "2025-05", can_complete=True)
    backfill._fetch_month("LGA", "2025-05", can_complete=True)
    backfill.close()

    keys = {path.name for path in (tmp_path / "raw").rglob("asos_obs/*.jsonl.gz")}
    assert keys == {"NYC_2025-05.jsonl.gz", "LGA_2025-05.jsonl.gz"}


def test_asos_resume_after_kill_skips_completed_station(tmp_path: Path) -> None:
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
            "stations": ["NYC", "LGA"],
            "network": "NY_ASOS",
        },
    }
    backfill = AsosObsBackfill(config)
    conn = connect(config["storage"]["backfill_db"])
    init_backfill_schema(conn)
    init_state_schema(conn)
    set_asos_month_complete(conn, "NYC", "2025-05", utc_now_iso())
    conn.close()

    calls: list[str] = []

    class RecordingClient:
        def close(self) -> None:
            return None

        def get(self, path: str, *, params=None) -> RequestResult:
            station = (params or {}).get("station")
            calls.append(str(station))
            return RequestResult(
                status_code=200,
                latency_ms=1,
                json_body=None,
                error_text=None,
                endpoint=path,
                text_body="station,valid,tmpf\n",
            )

    backfill.client = RecordingClient()  # type: ignore[assignment]
    backfill._fetch_month("NYC", "2025-05", can_complete=True)
    backfill._fetch_month("LGA", "2025-05", can_complete=True)
    backfill.close()

    assert calls == ["LGA"]


def test_load_asos_legacy_key_is_nyc_only(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    fixture = Path(__file__).resolve().parent / "fixtures" / "asos_nyc_sample.csv"
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2025-05",
        http_status=200,
        latency_ms=1,
        payload={"text": fixture.read_text(encoding="utf-8"), "month": "2025-05"},
    )
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="LGA_2025-05",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nLGA,2025-05-01 12:00,72.0\n",
            "month": "2025-05",
            "station": "LGA",
        },
    )
    writer.close()

    nyc = load_asos_observations_from_raw(raw_dir, station="NYC")
    lga = load_asos_observations_from_raw(raw_dir, station="LGA")
    assert len(nyc) == 4
    assert len(lga) == 1
