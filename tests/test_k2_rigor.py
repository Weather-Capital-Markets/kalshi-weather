"""Tests for K2 rigor helpers (no live data)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from analysis.k2_rigor import (
    classify_print,
    cli_revision_table,
    quadratic_taker_fee,
    settlement_era,
    settlement_snapshot_utc,
)
from analysis.murphy import murphy_decompose
from ingestion.nbm_archive import parse_percentile_levels


def test_quadratic_taker_fee_peaks_at_half() -> None:
    at_half = quadratic_taker_fee(0.50)
    assert at_half == pytest.approx(0.0175)
    at_tenth = quadratic_taker_fee(0.10)
    assert at_tenth == pytest.approx(0.0063)
    assert quadratic_taker_fee(0.10) == quadratic_taker_fee(0.90)
    assert at_half > at_tenth
    assert quadratic_taker_fee(0.0) == 0.0
    assert quadratic_taker_fee(1.0) == 0.0


def test_murphy_perfect_forecast_zero_brier() -> None:
    yes = np.array([1.0, 0.0, 1.0, 0.0])
    pred = np.array([1.0, 0.0, 1.0, 0.0])
    out = murphy_decompose(yes, pred, n_bins=4)
    assert out["brier"] == pytest.approx(0.0)
    assert out["reconstructed"] == pytest.approx(0.0, abs=1e-12)


def test_murphy_climatology_is_pure_uncertainty() -> None:
    yes = np.array([1.0, 0.0, 1.0, 0.0])
    pred = np.array([0.5, 0.5, 0.5, 0.5])
    out = murphy_decompose(yes, pred, n_bins=2)
    assert out["brier"] == pytest.approx(0.25)
    assert out["uncertainty"] == pytest.approx(0.25)
    assert out["resolution"] == pytest.approx(0.0)


def test_settlement_era_bounds() -> None:
    assert settlement_era("2021-12-25") == "unspecified_assume_10am"
    assert settlement_era("2021-12-28") == "first_10am"
    assert settlement_era("2024-09-03") == "first_10am"
    assert settlement_era("2024-09-04") == "first_7_or_8am"


def test_settlement_snapshot_is_next_morning_civil() -> None:
    snap = settlement_snapshot_utc("2026-01-15", 10)
    assert snap.tzinfo is not None
    assert snap == datetime(2026, 1, 16, 15, 0, tzinfo=timezone.utc)


def test_cli_revision_detects_late_change() -> None:
    labels = pd.DataFrame(
        {
            "issuance_ts_utc": [
                "2024-09-05T11:00:00Z",
                "2024-09-05T16:00:00Z",
            ],
            "climate_date": ["2024-09-04", "2024-09-04"],
            "high_F": [80, 82],
            "is_same_day_intermediate": [False, False],
        }
    )
    table = cli_revision_table(labels)
    assert len(table) == 1
    row = table.iloc[0]
    assert row["era"] == "first_7_or_8am"
    assert row["snap_08_high_f"] == 80
    assert bool(row["snap_08_disagree"]) is True
    assert row["last_high_f"] == 82


def test_classify_print_at_bid_and_inside() -> None:
    assert classify_print(0.40, 0.40, 0.44) == "at_bid"
    assert classify_print(0.44, 0.40, 0.44) == "at_ask"
    assert classify_print(0.42, 0.40, 0.44) == "inside"
    assert classify_print(0.50, 0.40, 0.44) == "outside"
    assert classify_print(0.40, None, 0.44) == "no_book"


def test_parse_percentile_levels_range_and_list() -> None:
    assert parse_percentile_levels("1-3") == [1, 2, 3]
    assert parse_percentile_levels("10,20,90") == [10, 20, 90]
