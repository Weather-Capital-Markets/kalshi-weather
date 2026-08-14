"""Tests for Clock B calibration table."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.clockb_check import build_clockb_table
from ingestion.writer import RawJsonlWriter, utc_now_iso


def test_clockb_table_reports_both_hypotheses(tmp_path: Path) -> None:
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
    writer.close()
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2025-05-01",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2025-05-02T10:00:00Z",
            }
        ]
    )
    table = build_clockb_table(labels=labels, raw_dir=raw_dir, sample_days=1)
    assert "consistent_lst" in table.columns
    assert "consistent_ldt" in table.columns
    assert len(table) == 1
