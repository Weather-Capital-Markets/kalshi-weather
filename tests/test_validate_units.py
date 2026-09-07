"""Tests for unit-range guards and non-empty output assertions."""

from __future__ import annotations

import pandas as pd
import pytest

from ingestion.validate_units import (
    assert_non_empty_frame,
    assert_non_empty_rows,
    csv_has_data_rows,
    price_as_dollars,
    validate_probability,
    validate_temperature_f,
)


def test_validate_temperature_f_accepts_plausible_range() -> None:
    assert validate_temperature_f(32.0) == 32.0
    assert validate_temperature_f(-30.0) == -30.0
    assert validate_temperature_f(130.0) == 130.0


def test_validate_temperature_f_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="outside plausible"):
        validate_temperature_f(131.0)
    with pytest.raises(ValueError, match="outside plausible"):
        validate_temperature_f(-31.0)


def test_validate_probability_bounds() -> None:
    assert validate_probability(0.0) == 0.0
    assert validate_probability(1.0) == 1.0
    with pytest.raises(ValueError, match="outside"):
        validate_probability(1.01)


def test_price_as_dollars_auto_detects_cents() -> None:
    assert price_as_dollars(45, unit="auto") == pytest.approx(0.45)
    assert price_as_dollars(0.45, unit="auto") == pytest.approx(0.45)
    assert price_as_dollars(99, unit="cents") == pytest.approx(0.99)


def test_csv_has_data_rows() -> None:
    assert not csv_has_data_rows("")
    assert not csv_has_data_rows("station,valid,tmpf\n")
    assert csv_has_data_rows("station,valid,tmpf\nNYC,2025-05-01 12:00,70.0\n")


def test_assert_non_empty_outputs() -> None:
    assert_non_empty_rows(1, what="rows")
    with pytest.raises(RuntimeError, match="refusing to write empty"):
        assert_non_empty_rows(0, what="rows")
    frame = pd.DataFrame([{"a": 1}])
    assert_non_empty_frame(frame, what="frame") is frame
    with pytest.raises(RuntimeError, match="refusing to write empty"):
        assert_non_empty_frame(pd.DataFrame(), what="frame")
