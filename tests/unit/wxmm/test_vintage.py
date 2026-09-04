"""Vintage snapshot identity and NBM plumbing constants."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from wxmm.data.ingest import nbm_archive_pointer
from wxmm.data.vintage import (
    NBM_PUBLICATION_P90_MIN,
    VintageStore,
    nbm_window_percentile_count,
    snapshot_id_for,
)

UTC = timezone.utc


def test_snapshot_id_is_content_addressed() -> None:
    store = VintageStore()
    store.write(
        "k",
        {"x": 1},
        valid_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
        source="test",
        ingest_run_id="run-1",
    )
    snap = store.snapshot()
    assert snap.snapshot_id == snapshot_id_for(snap.records)
    store2 = VintageStore()
    store2.write(
        "k",
        {"x": 1},
        valid_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
        source="test",
        ingest_run_id="run-1",
    )
    assert store2.snapshot().snapshot_id == snap.snapshot_id


def test_nbm_latency_is_measured_not_sixty() -> None:
    assert NBM_PUBLICATION_P90_MIN == 441
    assert nbm_window_percentile_count(date(2026, 5, 3)) == 99
    assert nbm_window_percentile_count(date(2026, 5, 5)) == 21
    pointer = nbm_archive_pointer()
    assert pointer["access"] == "idx_byte_range_never_whole_file"
    assert pointer["decode_module"] == "ingestion.nbm_archive"


def test_persist_parquet_roundtrip(tmp_path: Path) -> None:
    import duckdb

    store = VintageStore()
    store.write(
        "k",
        {"x": 1},
        valid_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
        source="test",
        ingest_run_id="run-1",
    )
    path = tmp_path / "v.parquet"
    store.persist_parquet(path)
    conn = duckdb.connect(":memory:")
    try:
        n = conn.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()
        assert n is not None
        assert n[0] == 1
    finally:
        conn.close()
