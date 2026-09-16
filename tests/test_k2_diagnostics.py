"""Unit tests for K2 diagnostic helpers (no live data)."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis.k2_diagnostics import brier_pair, pit_row, taker_edges
from analysis.murphy import paired_brier_diff


def _ladder(*, repaired: bool = False) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "percentile_level": [10, 50, 90],
            "value_f": [70.0, 75.0, 80.0],
            "isotonic_adjusted": [False, repaired, False],
        }
    )


def test_pit_row_bins_and_repair_flag() -> None:
    ladder = _ladder(repaired=True)
    below = pit_row(ladder, 65.0)
    assert below["below_p10"] is True
    assert below["in_p10_p90"] is False
    assert below["above_p90"] is False
    assert below["isotonic_repaired"] is True
    assert below["high_minus_p50"] == pytest.approx(-10.0)

    inside = pit_row(ladder, 75.0)
    assert inside["below_p10"] is False
    assert inside["in_p10_p90"] is True
    assert inside["above_p90"] is False
    assert inside["pit_cdf"] == pytest.approx(0.50, abs=0.01)

    above = pit_row(ladder, 85.0)
    assert above["above_p90"] is True
    assert above["in_p10_p90"] is False


def test_pit_row_missing_high() -> None:
    empty = pit_row(_ladder(), None)
    assert empty["below_p10"] is None
    assert empty["pit_cdf"] is None


def test_taker_edges_after_half_spread() -> None:
    sell, buy = taker_edges(mid=0.60, nbm=0.50, spread=0.04)
    assert sell == pytest.approx(0.08)
    assert buy == pytest.approx(-0.12)
    # Tight book: 2c of 10c mid-edge survives as taker-sell.
    sell_tight, buy_tight = taker_edges(mid=0.55, nbm=0.50, spread=0.02)
    assert sell_tight == pytest.approx(0.04)
    assert buy_tight == pytest.approx(-0.06)


def test_brier_pair_on_settled_rows() -> None:
    frame = pd.DataFrame(
        {
            "settled_yes": [1.0, 0.0, None],
            "nbm_prob": [0.8, 0.2, 0.5],
            "mid": [0.9, 0.1, 0.5],
        }
    )
    nbm, market = brier_pair(frame, "nbm_prob", "mid")
    assert nbm == pytest.approx(0.04)
    assert market == pytest.approx(0.01)


def test_paired_brier_diff_sign_and_ci() -> None:
    yes = pd.Series([1.0, 0.0, 1.0, 0.0]).to_numpy()
    worse = pd.Series([0.1, 0.9, 0.1, 0.9]).to_numpy()
    better = pd.Series([0.9, 0.1, 0.9, 0.1]).to_numpy()
    out = paired_brier_diff(yes, worse, better, n_boot=200, seed=1)
    assert out["mean"] > 0
    assert out["ci_lo"] > 0
    assert out["frac_a_better"] == pytest.approx(0.0)
