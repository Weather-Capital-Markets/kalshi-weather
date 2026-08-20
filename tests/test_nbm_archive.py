"""Tests for NBM archive backfill (mocked HTTP, no whole-file download)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ingestion.nbm_archive import (
    NbmArchiveBackfill,
    NbmArchiveClient,
    snapshot_utc_for_climate_date,
)
from ingestion.nbm_idx import ByteRange, IdxLine, vintage_select_cycle
from ingestion.state import nbm_day_complete, set_nbm_day_complete


def _idx_line(offset: int, level: int) -> IdxLine:
    return IdxLine(
        msg_number=level,
        byte_offset=offset,
        cycle_tag="2022070412",
        window_stat="0-18 hour max fcst",
        tail=f"{level}% level",
        raw_line=f"msg at p{level}",
    )


def _byte_range(start: int, end: int, level: int) -> ByteRange:
    return ByteRange(start=start, end=end, idx_line=_idx_line(start, level))


@pytest.fixture
def nbm_config(tmp_path: Path) -> dict:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    backfill_db = tmp_path / "backfill.sqlite"
    return {
        "storage": {
            "raw_dir": str(raw_dir),
            "backfill_db": str(backfill_db),
        },
        "nbm_archive": {
            "decoded_dir": str(tmp_path / "decoded"),
            "start_date": "2022-07-04",
            "end_date": "2022-07-04",
        },
    }


def test_snapshot_utc_is_t_minus_horizon_before_climate_day_end() -> None:
    climate = date(2022, 7, 4)
    snapshot = snapshot_utc_for_climate_date(climate, 24)
    assert snapshot == datetime(2022, 7, 4, 5, 0, tzinfo=timezone.utc)


def test_vintage_latency_blocks_same_hour_cycle() -> None:
    snapshot = datetime(2022, 7, 4, 12, 20, tzinfo=timezone.utc)
    candidates = [datetime(2022, 7, 4, 12, 0, tzinfo=timezone.utc)]
    assert vintage_select_cycle(snapshot, candidates, latency_min=60) is None


def test_fetch_percentile_ladder_uses_byte_ranges_only(nbm_config: dict, monkeypatch) -> None:
    app = NbmArchiveBackfill(nbm_config)
    idx_text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")

    def fake_fetch_text(url: str) -> tuple[int, str]:
        if url.endswith(".idx"):
            return 200, idx_text
        raise AssertionError(f"unexpected full grib fetch: {url}")

    def fake_fetch_range(url: str, byte_range: ByteRange) -> tuple[int, bytes]:
        level = byte_range.idx_line.percentile_level or 0
        return 206, f"grib-{level}".encode()

    def fake_decode(_grib_bytes: bytes, *, row: int, col: int) -> float:
        return 70.0 + row + col

    monkeypatch.setattr("ingestion.nbm_archive.decode_message_at_gridpoint", fake_decode)

    app.http.fetch_text = fake_fetch_text
    app.http.fetch_range = fake_fetch_range
    app._ensure_grid = MagicMock(return_value=(0, 0))
    app._grid_meta = (40.78, -73.97, 0.5)

    ladder, ranges, _ = app.fetch_percentile_ladder(
        datetime(2022, 7, 4, 12, tzinfo=timezone.utc),
        18,
    )
    assert len(ranges) == 4
    assert len(ladder) == 4
    assert {entry["percentile_level"] for entry in ladder} == {1, 2, 50, 99}
    app.close()


def test_process_climate_date_writes_parquet_and_raw(nbm_config: dict, monkeypatch) -> None:
    app = NbmArchiveBackfill(nbm_config)
    idx_text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")

    def fake_fetch_text(url: str) -> tuple[int, str]:
        if url.endswith(".idx"):
            return 200, idx_text
        return 404, ""

    def fake_fetch_range(url: str, byte_range: ByteRange) -> tuple[int, bytes]:
        return 206, b"bytes"

    def fake_decode(_grib_bytes: bytes, *, row: int, col: int) -> float:
        return 80.0 + row + col

    monkeypatch.setattr(
        "ingestion.nbm_archive.decode_message_at_gridpoint",
        fake_decode,
    )
    monkeypatch.setattr(
        "ingestion.nbm_archive.era_level_count_for_date",
        lambda _day: 4,
    )
    app.http.fetch_text = fake_fetch_text
    app.http.fetch_range = fake_fetch_range
    app._ensure_grid = MagicMock(return_value=(0, 0))
    app._grid_meta = (40.78, -73.97, 0.5)
    app.vintage_cycle_for_climate_date = MagicMock(
        return_value=datetime(2022, 7, 4, 12, tzinfo=timezone.utc),
    )

    result = app.process_climate_date(date(2022, 7, 4))
    assert result["status"] == "ok"
    parquet_path = Path(result["decoded_path"])
    assert parquet_path.exists()
    app.close()


def test_backfill_skips_completed_days(nbm_config: dict) -> None:
    app = NbmArchiveBackfill(nbm_config)
    set_nbm_day_complete(app.conn, "2022-07-04", "2026-01-01T00:00:00Z")
    called = False

    def fake_process(_climate_date: date) -> dict:
        nonlocal called
        called = True
        return {"status": "ok"}

    app.process_climate_date = fake_process
    app.backfill()
    assert not called
    assert nbm_day_complete(app.conn, "2022-07-04")
    app.close()


def test_dry_run_prints_bounds(nbm_config: dict) -> None:
    app = NbmArchiveBackfill(nbm_config)
    assert app.dry_run() == 0
    app.close()


def test_nbm_archive_client_range_header() -> None:
    line = _idx_line(100, 50)
    byte_range = ByteRange(start=100, end=199, idx_line=line)
    assert byte_range.header_value() == "bytes=100-199"
    client = NbmArchiveClient("https://example.com")
    client.close()
