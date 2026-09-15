"""GEFS archive client and vintage gates (mocked HTTP)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ingestion.gefs_archive import (
    GefsArchiveBackfill,
    GefsArchiveClient,
    LatencyNotPinnedError,
    gefs_select_watch_cycles,
    require_latency,
)
from ingestion.gefs_idx import parse_idx_text, select_tmp_2m_lines


def test_require_latency_refuses_unset() -> None:
    with pytest.raises(LatencyNotPinnedError):
        require_latency(None)
    assert require_latency(240) == 240


def test_fetch_retries_http_503(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GefsArchiveClient()
    client.max_retries = 3
    calls = {"n": 0}

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.content = b"ok"
            self.text = "ok"
            self.headers: dict[str, str] = {}

    def fake_get(url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(503)
        return FakeResponse(200)

    monkeypatch.setattr(client.client, "get", fake_get)
    monkeypatch.setattr("ingestion.gefs_archive.time.sleep", lambda _s: None)
    try:
        status, text = client.fetch_text("https://example.invalid/file.idx")
        assert status == 200
        assert text == "ok"
        assert calls["n"] == 3
    finally:
        client.close()


def test_fetch_range_rejects_idx_url() -> None:
    client = GefsArchiveClient()
    try:
        with pytest.raises(ValueError, match="idx url"):
            client.fetch_range("https://example.invalid/file.idx", "bytes=0-10")
    finally:
        client.close()
    client = GefsArchiveClient()
    try:
        with pytest.raises(ValueError, match="idx url"):
            client.fetch_range("https://example.invalid/file.idx", "bytes=0-10")
    finally:
        client.close()


def test_backfill_refuses_unset_latency(tmp_path: Path) -> None:
    config = {
        "storage": {"raw_dir": str(tmp_path / "raw")},
        "gefs_archive": {
            "decoded_dir": str(tmp_path / "decoded"),
            "backfill_db": str(tmp_path / "gefs.sqlite"),
            "nbm_sample_dir": str(tmp_path / "nbm"),
            "publication_latency_min": None,
        },
    }
    (tmp_path / "nbm").mkdir()
    app = GefsArchiveBackfill(config)
    try:
        with pytest.raises(LatencyNotPinnedError):
            app.backfill()
    finally:
        app.close()


def test_process_climate_date_uses_range_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx_text = Path("tests/fixtures/gefs_idx_pgrb2a_f006.txt").read_text(encoding="utf-8")
    config = {
        "storage": {"raw_dir": str(tmp_path / "raw")},
        "gefs_archive": {
            "decoded_dir": str(tmp_path / "decoded"),
            "backfill_db": str(tmp_path / "gefs.sqlite"),
            "nbm_sample_dir": str(tmp_path / "nbm"),
            "publication_latency_min": 240,
            "publication_latency_max_min": 240,
        },
    }
    app = GefsArchiveBackfill(config)
    seen_range = {"n": 0}

    def fake_fetch_text(url: str) -> tuple[int, str]:
        assert url.endswith(".idx")
        return 200, idx_text

    def fake_fetch_range(url: str, header_value: str) -> tuple[int, bytes]:
        assert not url.endswith(".idx")
        assert header_value.startswith("bytes=")
        seen_range["n"] += 1
        return 206, b"GRIB"

    monkeypatch.setattr(app.http, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(app.http, "fetch_range", fake_fetch_range)
    monkeypatch.setattr("ingestion.gefs_archive.MEMBERS", ("gec00",))
    monkeypatch.setattr(
        "ingestion.gefs_archive.nearest_gridpoint_from_grib",
        lambda *_args, **_kwargs: (0, 0, 41.0, -74.0, 25.0),
    )
    monkeypatch.setattr(
        "ingestion.gefs_archive.decode_grid_cells",
        lambda *_args, **_kwargs: {"cell": 72.0},
    )

    try:
        result = app.process_climate_date(date(2022, 12, 15), persist=False)
        assert result["n_members"] == 1
        assert result["members"][0]["tmax_f"] == 72.0
        assert seen_range["n"] == 8
        assert result["publication_latency_min"] == 240
    finally:
        app.close()


def test_fetch_text_rejects_grib_url() -> None:
    client = GefsArchiveClient()
    try:
        with pytest.raises(ValueError, match="only allowed for .idx"):
            client.fetch_text("https://example.invalid/file.pgrb2a.0p50.f006")
    finally:
        client.close()


def test_gefs_watch_cycles_near_now_not_tomorrow() -> None:
    now = datetime(2026, 8, 31, 1, 40, tzinfo=timezone.utc)
    cycles = gefs_select_watch_cycles(
        from_time=now,
        min_cycles=4,
        completed_nominals=set(),
    )
    nominals = [item[0] for item in cycles]
    assert nominals == [
        datetime(2026, 8, 30, 18, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 6, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 12, tzinfo=timezone.utc),
    ]


def test_tmp_selected_from_fixture_not_tmax() -> None:
    lines = parse_idx_text(Path("tests/fixtures/gefs_idx_pgrb2a_f006.txt").read_text())
    tmp = select_tmp_2m_lines(lines)
    assert tmp[0].var_name == "TMP"
    assert "max fcst" not in tmp[0].forecast
