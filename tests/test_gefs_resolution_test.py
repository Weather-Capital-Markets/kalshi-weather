"""GEFS member probabilities, vintage-adjacent Murphy fixture, Laplace analytic case."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from analysis.bracket_enumeration import ParsedStrike
from analysis.gefs_resolution_test import member_bracket_probabilities
from analysis.murphy import murphy_decompose
from ingestion.gefs_idx import VintageAvailabilityError, assert_vintage_available


def test_laplace_all_members_at_72() -> None:
    strikes = [
        ParsedStrike(role="between", floor_f=70, cap_f=71, width_f=2, strike_source="test"),
        ParsedStrike(role="between", floor_f=72, cap_f=73, width_f=2, strike_source="test"),
        ParsedStrike(role="between", floor_f=74, cap_f=75, width_f=2, strike_source="test"),
    ]
    tmaxes = [72.0] * 31
    probs = member_bracket_probabilities(tmaxes, strikes)
    assert probs[0] == pytest.approx(1.0 / 34.0)
    assert probs[1] == pytest.approx(32.0 / 34.0)
    assert probs[2] == pytest.approx(1.0 / 34.0)
    assert sum(probs) == pytest.approx(1.0)


def test_murphy_hand_computed_four_rows() -> None:
    yes = np.array([0.0, 0.0, 1.0, 1.0])
    pred = np.array([0.1, 0.3, 0.7, 0.9])
    out = murphy_decompose(yes, pred, n_bins=4)
    assert out["uncertainty"] == pytest.approx(0.25)
    assert out["brier"] == pytest.approx(0.05)
    assert out["reliability"] == pytest.approx(0.05)
    assert out["resolution"] == pytest.approx(0.25)
    assert out["reconstructed"] == pytest.approx(out["brier"])


def test_vintage_one_minute_after_snapshot_raises() -> None:
    snapshot = datetime(2022, 12, 15, 5, 0, tzinfo=timezone.utc)
    cycle = datetime(2022, 12, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(VintageAvailabilityError):
        assert_vintage_available(cycle, snapshot, latency_p90_min=301)
