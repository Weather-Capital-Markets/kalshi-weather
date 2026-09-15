"""Tests for NBM window mismatch measurement."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.window_mismatch import (
    _mismatch_rows,
    _parse_cli_high_f,
    build_k2_day_rows,
    summarize_by_season,
    summarize_k2_by_season,
)


def test_mismatch_rows_under_lst_and_ldt() -> None:
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    lst = _mismatch_rows(labels, "lst")
    ldt = _mismatch_rows(labels, "ldt")
    assert len(lst) == 1
    assert len(ldt) == 1
    summary = summarize_by_season(pd.concat([lst, ldt], ignore_index=True))
    assert set(summary["convention"]) == {"lst", "ldt"}


def test_parse_cli_high_f_rejects_missing_and_nan() -> None:
    class NanRow:
        high_F = float("nan")

    class EmptyRow:
        high_F = ""

    class MmRow:
        high_F = "MM"

    class MissingRow:
        pass

    class OkRow:
        high_F = 86

    assert _parse_cli_high_f(NanRow()) is None
    assert _parse_cli_high_f(EmptyRow()) is None
    assert _parse_cli_high_f(MmRow()) is None
    assert _parse_cli_high_f(MissingRow()) is None
    assert _parse_cli_high_f(OkRow()) == 86.0
    pandas_nan = pd.DataFrame([{"high_F": float("nan")}]).iloc[0]
    assert _parse_cli_high_f(pandas_nan) is None


def test_k2_rows_include_asos_and_both_cli_hypotheses(tmp_path: Path) -> None:
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "high_F": 86,
                "time_of_high_raw": "455 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    raw_dir = tmp_path / "raw"
    from ingestion.writer import RawJsonlWriter, utc_now_iso

    writer = RawJsonlWriter(raw_dir)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2026-07",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nNYC,2026-07-04 18:00,86.0\n",
            "month": "2026-07",
        },
    )
    writer.close()

    frame = build_k2_day_rows(labels, raw_dir)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert not row["cli_outside_lst"]
    assert not row["asos_outside_window"]
    assert row["delta_f"] == 0
    assert not row["bracket_disagree"]

    summary = summarize_k2_by_season(frame)
    assert "cli_outside_rate_lst" in summary.columns
    assert summary.loc[summary["season"] == "ALL", "n_days"].iloc[0] == 1


def test_k2_asos_outside_window_flags_correctly(tmp_path: Path) -> None:
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "high_F": 85,
                "time_of_high_raw": "455 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    raw_dir = tmp_path / "raw"
    from ingestion.writer import RawJsonlWriter, utc_now_iso

    writer = RawJsonlWriter(raw_dir)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2026-07",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nNYC,2026-07-04 08:00,90.0\n",
            "month": "2026-07",
        },
    )
    writer.close()

    frame = build_k2_day_rows(labels, raw_dir)
    assert bool(frame.iloc[0]["asos_outside_window"])
    assert bool(frame.iloc[0]["bracket_disagree"])


def test_k2_rows_keep_asos_when_cli_high_is_nan(tmp_path: Path) -> None:
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "high_F": float("nan"),
                "time_of_high_raw": "455 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    raw_dir = tmp_path / "raw"
    from ingestion.writer import RawJsonlWriter, utc_now_iso

    writer = RawJsonlWriter(raw_dir)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="2026-07",
        http_status=200,
        latency_ms=1,
        payload={
            "text": "station,valid,tmpf\nNYC,2026-07-04 18:00,86.0\n",
            "month": "2026-07",
        },
    )
    writer.close()

    frame = build_k2_day_rows(labels, raw_dir)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert pd.isna(row["cli_high_f"]) or row["cli_high_f"] is None
    assert pd.isna(row["cli_whole_f"]) or row["cli_whole_f"] is None
    assert pd.isna(row["delta_f"]) or row["delta_f"] is None
    assert pd.isna(row["bracket_disagree"]) or row["bracket_disagree"] is None
    assert row["asos_whole_f"] == 86


def test_window_mismatch_refuses_headline_when_unknown(tmp_path: Path, monkeypatch) -> None:
    from analysis import window_mismatch as module

    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    labels_csv = tmp_path / "clinyc.csv"
    labels_csv.write_text("climate_date\n", encoding="utf-8")

    def fake_select(_path):
        return labels

    monkeypatch.setattr(module, "select_full_day_labels", fake_select)
    monkeypatch.setattr(module, "run_k2", lambda *_args, **_kwargs: 0)
    captured: list[str] = []

    def capture_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", capture_print)
    module.run(
        {
            "storage": {"labels_csv": str(labels_csv), "raw_dir": str(tmp_path / "raw")},
            "climate": {"cli_time_convention": "unknown"},
        },
        tmp_path / "out",
    )
    assert any("REFUSED" in line for line in captured)

