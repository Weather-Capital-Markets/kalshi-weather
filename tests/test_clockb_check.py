"""Tests for Clock B calibration table."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.clockb_check import build_clockb_table, run
from ingestion.writer import RawJsonlWriter, utc_now_iso


def _write_asos_fixture(raw_dir: Path) -> None:
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
    writer.close()


def test_clockb_table_uses_all_labeled_days_with_asos(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    _write_asos_fixture(raw_dir)
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2025-05-01",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2025-05-02T10:00:00Z",
            },
            {
                "climate_date": "2025-06-02",
                "time_of_high_raw": "400 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2025-06-03T10:00:00Z",
            },
        ]
    )
    table = build_clockb_table(labels=labels, raw_dir=raw_dir)
    assert "delta_lst" in table.columns
    assert "delta_ldt" in table.columns
    assert "dst_status" in table.columns
    assert len(table) == 1
    assert table.iloc[0]["climate_date"] == "2025-05-01"


def test_clockb_run_splits_edt_and_est(tmp_path: Path, capsys) -> None:
    raw_dir = tmp_path / "raw"
    writer = RawJsonlWriter(raw_dir)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2026-07",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nNYC,2026-07-04 18:51,85.00\n",
            "month": "2026-07",
        },
    )
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2026-01",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nNYC,2026-01-10 15:51,40.00\n",
            "month": "2026-01",
        },
    )
    writer.close()
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            },
            {
                "climate_date": "2026-01-10",
                "time_of_high_raw": "200 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-01-11T10:00:00Z",
            },
        ]
    )
    labels_csv = tmp_path / "clinyc.csv"
    labels.to_csv(labels_csv, index=False)
    config = {
        "storage": {
            "raw_dir": str(raw_dir),
            "labels_csv": str(labels_csv),
        },
        "clockb_check": {"out_dir": str(tmp_path / "out")},
    }
    assert run(config, tmp_path / "out") == 0
    out = capsys.readouterr().out
    assert "measurement only" in out
    assert "EDT days (identifying)" in out
    assert "EST days (non-identifying" in out
    assert (tmp_path / "out" / "clockb_delta_lst_edt.png").exists()
