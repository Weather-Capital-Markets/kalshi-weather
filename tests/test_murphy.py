"""Unit tests for Murphy decomposition and K2 bootstrap helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.murphy import (
    bootstrap_mean_ci,
    day_clustered_brier_diff,
    murphy_decompose,
    murphy_model_rows,
    paired_brier_diff,
)


def test_murphy_perfect_forecast_zero_brier() -> None:
    yes = np.array([1.0, 0.0, 1.0, 0.0])
    pred = np.array([1.0, 0.0, 1.0, 0.0])
    out = murphy_decompose(yes, pred, n_bins=4)
    assert out["brier"] == pytest.approx(0.0)
    assert out["reconstructed"] == pytest.approx(0.0, abs=1e-12)


def test_bootstrap_mean_ci_contains_mean() -> None:
    values = np.array([0.0, 1.0, 0.0, 1.0])
    out = bootstrap_mean_ci(values, n_boot=200, seed=1)
    assert out["n"] == 4
    assert out["mean"] == pytest.approx(0.5)
    assert out["ci_lo"] <= out["mean"] <= out["ci_hi"]


def test_paired_brier_diff_sign() -> None:
    yes = np.array([1.0, 0.0, 1.0, 0.0])
    worse = np.array([0.1, 0.9, 0.1, 0.9])
    better = np.array([0.9, 0.1, 0.9, 0.1])
    out = paired_brier_diff(yes, worse, better, n_boot=200, seed=1)
    assert out["mean"] > 0
    assert out["frac_a_better"] == pytest.approx(0.0)


def test_day_clustered_brier_diff_uses_days_not_contracts() -> None:
    frame = pd.DataFrame(
        {
            "climate_date": ["2022-12-15"] * 3 + ["2022-12-16"] * 3,
            "settled_yes": [1.0, 0.0, 1.0, 0.0, 1.0, 0.0],
            "nbm_prob": [0.9, 0.1, 0.9, 0.9, 0.1, 0.9],
            "market_mid_carryforward": [0.8, 0.2, 0.8, 0.2, 0.8, 0.2],
        }
    )
    out = day_clustered_brier_diff(frame, "nbm_prob", "market_mid_carryforward", n_boot=50, seed=1)
    assert out["n"] == 2.0
    assert np.isfinite(out["mean"])


def test_murphy_model_rows_skips_missing_columns() -> None:
    frame = pd.DataFrame(
        {
            "settled_yes": [1.0, 0.0],
            "nbm_prob": [0.8, 0.2],
        }
    )
    rows = murphy_model_rows(frame, [("nbm", "nbm_prob"), ("missing", "nope")])
    assert len(rows) == 1
    assert rows[0]["model"] == "nbm"
