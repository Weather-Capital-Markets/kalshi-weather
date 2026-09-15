"""Tests for NBM archive backfill (mocked HTTP, no whole-file download)."""

from __future__ import annotations

from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from ingestion.nbm_archive import (
    NbmArchiveBackfill,
    NbmArchiveClient,
    apply_archive_overrides,
    snapshot_utc_for_climate_date,
    stratified_sample_climate_dates,
    summarize_sample_strata,
)
from ingestion.nbm_idx import (
    ByteRange,
    IdxLine,
    VintageSelection,
    parse_idx_text,
    publication_utc,
    select_max_window_percentile_lines,
    vintage_select_cycle,
)
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
            "percentile_levels": [1, 2, 50, 99],
            "sample_size": 1,
            "publication_latency_min": 441,
            "publication_latency_max_min": 453,
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

    def fake_decode(_grib_bytes: bytes, cells):
        return {name: 70.0 + row + col for name, row, col in cells}

    monkeypatch.setattr("ingestion.nbm_archive.decode_grid_cells", fake_decode)

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
    idx_text = Path("tests/fixtures/nbm_idx_f042_12z_sample.txt").read_text(encoding="utf-8")
    climate = date(2022, 7, 4)
    vintage = datetime(2022, 7, 3, 12, tzinfo=timezone.utc)
    forecast_hour = 42
    snapshot = snapshot_utc_for_climate_date(climate, 24)
    pct_lines = select_max_window_percentile_lines(
        parse_idx_text(idx_text),
        forecast_hour=forecast_hour,
        percentile_levels={10, 20, 50, 90},
    )
    selection = VintageSelection(
        cycle=vintage,
        forecast_hour=forecast_hour,
        matched_lines=tuple(pct_lines),
        forecast_lead_h=24.0,
        publication_utc_p90=publication_utc(vintage, 441),
        publication_utc_max=publication_utc(vintage, 453),
        snapshot_utc=snapshot,
    )

    def fake_fetch_text(url: str) -> tuple[int, str]:
        if url.endswith(".idx"):
            return 200, idx_text
        return 404, ""

    def fake_fetch_range(url: str, byte_range: ByteRange) -> tuple[int, bytes]:
        return 206, b"bytes"

    def fake_decode(_grib_bytes: bytes, cells):
        return {name: 80.0 + row + col for name, row, col in cells}

    monkeypatch.setattr(
        "ingestion.nbm_archive.decode_grid_cells",
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
    app.percentile_levels = (10, 20, 50, 90)
    app.percentile_level_set = {10, 20, 50, 90}
    app.empirical_vintage_for_climate_date = MagicMock(return_value=selection)

    result = app.process_climate_date(climate)
    assert result["status"] == "ok"
    assert result["forecast_hour"] == 42
    assert result.get("matched_idx_lines")
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


def test_dry_run_prints_bounds(nbm_config: dict, monkeypatch) -> None:
    app = NbmArchiveBackfill(nbm_config)

    def fake_estimate(_climate_date: date) -> tuple[int, int]:
        return 48_600_000, 50_000

    monkeypatch.setattr(app, "estimate_day_grib_bytes_from_idx", fake_estimate)
    assert app.dry_run() == 0
    app.close()


def test_stratified_sample_is_deterministic_and_balanced() -> None:
    start = date(2021, 8, 5)
    end = date(2026, 5, 3)
    split = date(2024, 5, 15)
    first = stratified_sample_climate_dates(
        start,
        end,
        target_n=300,
        sub_era_split=split,
        seed=42,
    )
    second = stratified_sample_climate_dates(
        start,
        end,
        target_n=300,
        sub_era_split=split,
        seed=42,
    )
    assert first == second
    assert len(first) == 300
    strata = summarize_sample_strata(first, sub_era_split=split)
    assert len(strata) == 8
    assert min(strata.values()) >= 37
    assert max(strata.values()) <= 38


def test_percentile_level_filter_on_fixture() -> None:
    text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    pct = select_max_window_percentile_lines(
        lines,
        forecast_hour=18,
        percentile_levels={10, 20, 30},
    )
    assert pct == []
    pct_subset = select_max_window_percentile_lines(
        lines,
        forecast_hour=18,
        percentile_levels={1, 50, 99},
    )
    assert [line.percentile_level for line in pct_subset] == [1, 50, 99]


def test_nbm_archive_client_range_header() -> None:
    line = _idx_line(100, 50)
    byte_range = ByteRange(start=100, end=199, idx_line=line)
    assert byte_range.header_value() == "bytes=100-199"
    client = NbmArchiveClient("https://example.com")
    client.close()


def test_nbm_uses_dedicated_backfill_db_not_shared_storage(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shared = tmp_path / "backfill.sqlite"
    nbm_db = tmp_path / "backfill_v441.sqlite"
    config = {
        "storage": {
            "raw_dir": str(raw_dir),
            "backfill_db": str(shared),
        },
        "nbm_archive": {
            "backfill_db": str(nbm_db),
            "decoded_dir": str(tmp_path / "decoded"),
            "start_date": "2022-07-04",
            "end_date": "2022-07-04",
            "sample_size": 1,
        },
    }
    backfill = NbmArchiveBackfill(config)
    try:
        assert backfill.backfill_db_path == nbm_db
        assert nbm_db.exists()
        assert not shared.exists()
    finally:
        backfill.conn.close()


def test_nbm_archive_client_retries_timeout(monkeypatch) -> None:
    client = NbmArchiveClient("https://example.com")
    calls = {"n": 0}

    def fake_get(url: str, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("slow")
        response = MagicMock()
        response.status_code = 200
        response.content = b"ok"
        response.text = "ok"
        return response

    monkeypatch.setattr("ingestion.nbm_archive.time.sleep", lambda _s: None)
    client.client.get = fake_get
    status, text = client.fetch_text("https://example.com/x")
    assert status == 200
    assert text == "ok"
    assert calls["n"] == 3
    client.close()


def test_fetch_percentile_ladder_neighbor_columns(nbm_config: dict, monkeypatch) -> None:
    nbm_config["nbm_archive"]["decode_neighbors"] = True
    app = NbmArchiveBackfill(nbm_config)
    idx_text = Path("tests/fixtures/nbm_idx_v4_sample.txt").read_text(encoding="utf-8")

    def fake_fetch_text(url: str) -> tuple[int, str]:
        if url.endswith(".idx"):
            return 200, idx_text
        raise AssertionError(f"unexpected full grib fetch: {url}")

    def fake_fetch_range(url: str, byte_range: ByteRange) -> tuple[int, bytes]:
        return 206, b"grib"

    def fake_decode(_grib_bytes: bytes, cells):
        return {name: 70.0 + float(row) + float(col) for name, row, col in cells}

    monkeypatch.setattr("ingestion.nbm_archive.decode_grid_cells", fake_decode)
    app.http.fetch_text = fake_fetch_text
    app.http.fetch_range = fake_fetch_range
    app._ensure_grid = MagicMock(return_value=(5, 7))
    app._grid_meta = (40.78, -73.97, 0.5)
    ladder, _ranges, _ = app.fetch_percentile_ladder(
        datetime(2022, 7, 4, 12, tzinfo=timezone.utc),
        18,
    )
    assert ladder
    entry = ladder[0]
    assert entry["value_f_n"] == pytest.approx(70.0 + 4 + 7)
    assert entry["value_f_s"] == pytest.approx(70.0 + 6 + 7)
    assert entry["value_f_w"] == pytest.approx(70.0 + 5 + 6)
    assert entry["value_f_e"] == pytest.approx(70.0 + 5 + 8)
    app.close()


def test_apply_archive_overrides_uses_sidecar_db() -> None:
    config = {
        "nbm_archive": {"decoded_dir": "data/nbm/decoded_v441"},
        "storage": {"backfill_db": "data/backfill_v441.sqlite"},
    }
    args = Namespace(
        decoded_dir=Path("data/nbm/decoded_v441_p99"),
        horizon_h=24,
        percentile_levels="1-99",
        max_days=50,
        neighbor_cells=True,
        backfill_db=Path("data/backfill_v441_p99.sqlite"),
    )
    out = apply_archive_overrides(config, args)
    assert out["nbm_archive"]["decoded_dir"].endswith("decoded_v441_p99")
    assert out["nbm_archive"]["percentile_levels"] == list(range(1, 100))
    assert out["nbm_archive"]["decode_neighbors"] is True
    assert out["nbm_archive"]["max_days"] == 50
    assert str(out["storage"]["backfill_db"]).endswith("backfill_v441_p99.sqlite")
    assert config["storage"]["backfill_db"] == "data/backfill_v441.sqlite"


def test_max_days_subsamples_deterministically(nbm_config: dict) -> None:
    nbm_config["nbm_archive"]["start_date"] = "2021-08-05"
    nbm_config["nbm_archive"]["end_date"] = "2026-05-03"
    nbm_config["nbm_archive"]["sample_size"] = 300
    nbm_config["nbm_archive"]["sample_seed"] = 43
    nbm_config["nbm_archive"]["max_days"] = 50
    app = NbmArchiveBackfill(nbm_config)
    first = app.sampled_climate_dates()
    second = app.sampled_climate_dates()
    assert len(first) == 50
    assert first == second
    full_cfg = dict(nbm_config)
    full_cfg["nbm_archive"] = dict(nbm_config["nbm_archive"])
    del full_cfg["nbm_archive"]["max_days"]
    full = NbmArchiveBackfill(full_cfg)
    assert set(first).issubset(set(full.sampled_climate_dates()))
    app.close()
    full.close()
