"""Tests for NBM ladder dedupe and isotonic adjustment."""

from __future__ import annotations

from ingestion.nbm_ladder import (
    apply_isotonic_to_ladder,
    dedupe_ladder_by_percentile,
    isotonic_maximize,
)


def test_dedupe_keeps_first_per_percentile():
  ladder = [
    {"percentile_level": 10, "byte_offset": 100, "value_f": 70.0},
    {"percentile_level": 10, "byte_offset": 200, "value_f": 71.0},
    {"percentile_level": 50, "byte_offset": 300, "value_f": 75.0},
    {"percentile_level": 90, "byte_offset": 400, "value_f": 80.0},
    {"percentile_level": 90, "byte_offset": 500, "value_f": 81.0},
  ]
  deduped, n = dedupe_ladder_by_percentile(ladder)
  assert n == 2
  assert len(deduped) == 3
  assert deduped[0]["value_f"] == 70.0
  assert deduped[2]["value_f"] == 80.0


def test_isotonic_maximize_non_monotone():
  levels = [10, 50, 90, 100]
  values = [70.0, 75.0, 74.0, 80.0]
  adjusted = isotonic_maximize(levels, values)
  assert adjusted == [70.0, 74.5, 74.5, 80.0]


def test_isotonic_maximize_already_monotone():
  levels = [10, 50, 90, 100]
  values = [70.0, 75.0, 78.0, 80.0]
  adjusted = isotonic_maximize(levels, values)
  assert adjusted == values


def test_apply_isotonic_to_ladder():
  ladder = [
    {"percentile_level": 10, "byte_offset": 100, "value_f": 70.0},
    {"percentile_level": 50, "byte_offset": 200, "value_f": 75.0},
    {"percentile_level": 90, "byte_offset": 300, "value_f": 74.0},
  ]
  adjusted, n = apply_isotonic_to_ladder(ladder)
  assert n is True
  assert adjusted[0]["value_f_raw"] == 70.0
  assert adjusted[1]["value_f_raw"] == 75.0
  assert adjusted[2]["value_f_raw"] == 74.0
  assert adjusted[1]["value_f"] == 74.5
  assert adjusted[2]["value_f"] == 74.5
  assert adjusted[2]["isotonic_adjusted"] is True
  assert adjusted[0]["isotonic_adjusted"] is False
