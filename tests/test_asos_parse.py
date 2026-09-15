"""Tests for IEM ASOS CSV parsing."""

from __future__ import annotations

from pathlib import Path

from ingestion.asos_parse import parse_asos_csv


def test_parse_asos_csv_reads_tmpf_and_valid() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "asos_nyc_sample.csv"
    observations = parse_asos_csv(fixture.read_text(encoding="utf-8"))
    assert len(observations) == 4
    assert observations[-1].tmpf == 70.0
