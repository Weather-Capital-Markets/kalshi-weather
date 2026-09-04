"""Tests for KLGA–KNYC station basis measurement."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from analysis.station_basis import (
    NOTES_CAVEAT,
    build_paired_days,
    daily_max_table,
    disagree_by_knyc_max,
    polymarket_bracket,
    run,
    summarize_slice,
    whole_f,
    withdraw_summer_ladder_brackets,
)
from ingestion.climate_time import AsosObservation, asos_max_for_climate_day
from ingestion.writer import RawJsonlWriter, utc_now_iso


def test_whole_f_rounds_to_nearest_int() -> None:
    assert whole_f(78.0) == 78
    assert whole_f(77.4) == 77
    assert whole_f(77.6) == 78


def test_polymarket_bracket_edges() -> None:
    assert polymarket_bracket(75) == "tail_below"
    assert polymarket_bracket(76) == "between_76-77"
    assert polymarket_bracket(77) == "between_76-77"
    assert polymarket_bracket(whole_f(78.0)) == "between_78-79"
    assert polymarket_bracket(94) == "tail_above"


def test_asos_max_uses_lst_day_not_civil_midnight_dst_boundary() -> None:
    # LST climate day 2026-07-04: 2026-07-04 05:00Z .. 2026-07-05 05:00Z
    obs = [
        AsosObservation(datetime(2026, 7, 4, 20, 0, tzinfo=timezone.utc), 80.0),
        AsosObservation(datetime(2026, 7, 5, 4, 30, tzinfo=timezone.utc), 86.0),
        AsosObservation(datetime(2026, 7, 5, 5, 30, tzinfo=timezone.utc), 90.0),
    ]
    max_f, max_ts = asos_max_for_climate_day(obs, "2026-07-04")
    assert max_f == 86.0
    assert max_ts == obs[1].valid_utc

    next_max_f, _ = asos_max_for_climate_day(obs, "2026-07-05")
    assert next_max_f == 90.0


def test_delta_and_bracket_disagreement_arithmetic() -> None:
    paired = build_paired_days(
        knyc_maxes={"2025-05-01": 81},
        klga_maxes={"2025-05-01": 82},
        start=date(2025, 5, 1),
        end=date(2025, 5, 1),
    )
    assert len(paired) == 1
    row = paired.iloc[0]
    assert row["delta_f"] == 1
    assert row["knyc_bracket"] == "between_80-81"
    assert row["klga_bracket"] == "between_82-83"
    assert bool(row["bracket_disagree"]) is True


def test_summarize_slice_includes_headline_fraction() -> None:
    frame = pd.DataFrame(
        [
            {"delta_f": 0, "bracket_disagree": False},
            {"delta_f": 2, "bracket_disagree": True},
        ]
    )
    row = summarize_slice(frame, slice_name="overall")
    assert row["n_days"] == 2
    assert row["bracket_disagree_frac"] == 0.5
    assert row["share_delta_eq_0"] == 0.5
    assert row["share_klga_warmer"] == 0.5


def test_withdraw_summer_ladder_keeps_jja() -> None:
    jja = withdraw_summer_ladder_brackets(
        {"slice": "JJA", "bracket_disagree_frac": 0.12, "bracket_disagree_frac_middle": 0.1}
    )
    djf = withdraw_summer_ladder_brackets(
        {"slice": "DJF", "bracket_disagree_frac": 0.0, "bracket_disagree_frac_middle": 0.0}
    )
    assert jja["bracket_status"] == "measured"
    assert jja["bracket_disagree_frac"] == 0.12
    assert djf["bracket_status"] == "withdrawn_summer_ladder"
    assert pd.isna(djf["bracket_disagree_frac"])


def _write_station_fixtures(raw_dir: Path) -> None:
    writer = RawJsonlWriter(raw_dir)
    nyc = Path(__file__).resolve().parent / "fixtures" / "asos_nyc_sample.csv"
    lga = Path(__file__).resolve().parent / "fixtures" / "asos_lga_sample.csv"
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="NYC_2025-05",
        http_status=200,
        latency_ms=1,
        payload={"text": nyc.read_text(encoding="utf-8"), "month": "2025-05", "station": "NYC"},
    )
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/cgi-bin/request/asos.py",
        category="asos_obs",
        key="LGA_2025-05",
        http_status=200,
        latency_ms=1,
        payload={"text": lga.read_text(encoding="utf-8"), "month": "2025-05", "station": "LGA"},
    )
    writer.close()


def test_daily_max_table_from_fixture() -> None:
    obs = [
        AsosObservation(datetime(2025, 5, 1, 18, 0, tzinfo=timezone.utc), 78.0),
        AsosObservation(datetime(2025, 5, 1, 20, 0, tzinfo=timezone.utc), 81.0),
    ]
    maxes = daily_max_table(obs, start=date(2025, 5, 1), end=date(2025, 5, 1))
    assert maxes["2025-05-01"] == 81
    dropped = daily_max_table(obs, start=date(2025, 5, 1), end=date(2025, 5, 1), min_obs=12)
    assert dropped == {}


def test_disagree_by_knyc_max_spikes_at_bin_edges() -> None:
    frame = pd.DataFrame(
        [
            {"knyc_max_f": 85, "delta_f": 1, "bracket_disagree": True},
            {"knyc_max_f": 85, "delta_f": 1, "bracket_disagree": True},
            {"knyc_max_f": 85, "delta_f": 0, "bracket_disagree": False},
            {"knyc_max_f": 86, "delta_f": 0, "bracket_disagree": False},
            {"knyc_max_f": 86, "delta_f": 0, "bracket_disagree": False},
            {"knyc_max_f": 40, "delta_f": 1, "bracket_disagree": False},
            {"knyc_max_f": 40, "delta_f": 1, "bracket_disagree": False},
        ]
    )
    table = disagree_by_knyc_max(frame)
    row85 = table.loc[table["knyc_max_f"] == 85].iloc[0]
    row86 = table.loc[table["knyc_max_f"] == 86].iloc[0]
    row40 = table.loc[table["knyc_max_f"] == 40].iloc[0]
    assert row85["bracket_disagree_frac"] == pytest.approx(0.6667, abs=0.001)
    assert row86["bracket_disagree_frac"] == 0.0
    assert row40["bracket_disagree_frac"] == 0.0


def test_station_basis_run_writes_csv_and_notes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_dir = tmp_path / "raw"
    _write_station_fixtures(raw_dir)
    out_dir = tmp_path / "out"
    config = {
        "storage": {"raw_dir": str(raw_dir)},
        "asos_obs": {"start_date": "2025-05-01", "stations": ["NYC", "LGA"]},
        "station_basis": {
            "out_dir": str(out_dir),
            "start_date": "2025-05-01",
            "knyc_station": "NYC",
            "klga_station": "LGA",
        },
    }
    assert run(config, out_dir) == 0
    out = capsys.readouterr().out
    assert NOTES_CAVEAT.split(".")[0] in out
    assert "bracket_disagree_frac=" in out

    csv_path = out_dir / "station_basis.csv"
    assert csv_path.exists()
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert "lower bound" in header.lower()
    summary = pd.read_csv(csv_path, comment="#")
    assert "overall" in summary["slice"].values
    assert "DJF" in summary["slice"].values
    assert "ladder_interior" in summary["slice"].values
    djf = summary[summary["slice"] == "DJF"].iloc[0]
    assert djf["bracket_status"] == "withdrawn_summer_ladder"
    assert pd.isna(djf["bracket_disagree_frac"])
    assert (out_dir / "station_basis_delta_hist.png").exists()
    assert (out_dir / "station_basis_disagree_by_season.png").exists()
    assert (out_dir / "station_basis_delta_by_month.png").exists()
