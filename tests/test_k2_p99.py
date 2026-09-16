"""Unit tests for 99-level / neighbor helpers (no live NBM)."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis.k2_p99_neighbors import (
    neighbor_p50_errors,
    subsample_levels,
    tail_coverage,
)
from analysis.k2_t12_compare import _print_briers


def test_subsample_keeps_deciles_only() -> None:
    ladder = pd.DataFrame({"percentile_level": list(range(1, 100)), "value_f": range(1, 100)})
    sub = subsample_levels(ladder)
    assert sub["percentile_level"].tolist() == [10, 20, 30, 40, 50, 60, 70, 80, 90]


def test_tail_coverage_p1_p99() -> None:
    ladder = pd.DataFrame({"percentile_level": [1, 50, 99], "value_f": [60.0, 70.0, 80.0]})
    inside = tail_coverage(ladder, 70.0)
    assert inside["in_p1_p99"] is True
    assert inside["below_p1"] is False
    below = tail_coverage(ladder, 50.0)
    assert below["below_p1"] is True
    above = tail_coverage(ladder, 90.0)
    assert above["above_p99"] is True


def test_neighbor_closer_when_offset_cell_matches_obs() -> None:
    ladder = pd.DataFrame(
        {
            "percentile_level": [50],
            "value_f": [80.0],
            "value_f_n": [78.0],
            "value_f_s": [81.0],
            "value_f_w": [79.5],
            "value_f_e": [82.0],
        }
    )
    out = neighbor_p50_errors(ladder, 78.0)
    assert out["nearest_abs_err"] == pytest.approx(2.0)
    assert out["best_neighbor_abs_err"] == pytest.approx(0.0)
    assert out["neighbor_closer"] is True
    assert out["n_abs_err"] == pytest.approx(0.0)
    assert out["mean_cell_abs_err"] == pytest.approx(abs((78 + 81 + 79.5 + 82) / 4 - 78.0))


def test_t12_brier_print_on_tiny_frame() -> None:
    frame = pd.DataFrame(
        {
            "settled_yes": [1.0, 0.0],
            "nbm_prob_t12": [0.8, 0.2],
            "market_mid": [0.9, 0.1],
        }
    )
    row = _print_briers("unit", frame, "nbm_prob_t12", "market_mid")
    assert row["n"] == 2
    assert row["market_brier"] < row["nbm_brier"]
