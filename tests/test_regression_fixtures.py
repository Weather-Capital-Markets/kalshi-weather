"""Frozen regression fixtures with expected values from knowledge/ docs.

These tests pin small committed samples. If a published number in knowledge/
diverges from code on the fixture corpus, the test documents the finding in a
comment and fails — analysis logic must not be changed to match a wrong doc.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from analysis.bracket_enumeration import build_daily_table, event_structure
from analysis.spread_census import candle_fields, era_of, is_two_sided
from ingestion.cli_labels import parse_modern_product, split_products
from ingestion.nbm_idx import kelvin_to_fahrenheit as k2f
from ingestion.nbm_idx import parse_idx_text, select_max_window_percentile_lines
from ingestion.validate_units import validate_temperature_f

FIXTURES = Path(__file__).parent / "fixtures" / "regression"
EXPECTED = json.loads((FIXTURES / "expected_values.json").read_text(encoding="utf-8"))


def test_clinyc_fixture_matches_published_label_values() -> None:
    """knowledge/data-sources.md §1 — CLINYC parser on frozen July 4 2026 issuance."""
    text = (Path(__file__).parent / "fixtures" / "clinyc_cdus41.txt").read_text(encoding="utf-8")
    products = split_products(text)
    parsed = [parse_modern_product(p, source_month="2026-07") for p in products]
    parsed = [row for row in parsed if row]
    exp = EXPECTED["clinyc"]
    final = next(row for row in parsed if not row["is_same_day_intermediate"])
    intermediate = next(row for row in parsed if row["is_same_day_intermediate"])
    assert final["climate_date"] == exp["climate_date"]
    assert final["high_F"] == exp["final_high_F"]
    assert final["time_of_high_raw"] == exp["final_time_of_high_raw"]
    assert intermediate["time_of_high_raw"] == exp["intermediate_time_of_high_raw"]


def test_nbm_idx_v4_fixture_matches_published_level_grid() -> None:
    """data-sources.md §3.2 — v4 idx window max fcst percentile levels."""
    fixture = Path(__file__).parent / "fixtures" / "nbm_idx_v4_sample.txt"
    text = fixture.read_text(encoding="utf-8")
    lines = parse_idx_text(text)
    exp = EXPECTED["nbm_idx_v4"]
    pct = select_max_window_percentile_lines(lines, forecast_hour=exp["forecast_hour"])
    levels = sorted(line.percentile_level for line in pct)
    assert levels == exp["percentile_levels"]
    assert all(line.window_hours == tuple(exp["window_hours"]) for line in pct)


def test_modern_bracket_ladder_matches_published_six_bracket_structure() -> None:
    """data-sources.md §5.1 — stable 6-bracket ladder from 2022-12-11 onward."""
    payload = json.loads((FIXTURES / "markets_2022-12-11.json").read_text(encoding="utf-8"))
    markets = payload["markets"]
    row = event_structure("2022-12-11", markets)
    assert row is not None
    exp = EXPECTED["bracket_structure"]
    assert row["n_brackets"] == exp["modern_n_brackets"]
    assert row["interior_width_f"] == exp["modern_interior_width_f"]
    assert row["contiguous_interior"] is True


def test_era_split_matches_published_2021_vs_2022_boundary() -> None:
    """data-sources.md §5.1 — 2021 structurally different (~1.3 vs ~6 brackets/day)."""
    assert era_of("2021-08-05") == "2021_early"
    assert era_of("2022-01-01") == "2022_plus"
    assert era_of("2022-12-11") == "2022_plus"


def test_spread_census_a6_empty_book_on_fixture_candle() -> None:
    """venue-facts.md §1.6 — bid 0.00 / ask 1.00 is empty book, not a spread."""
    fields = candle_fields(
        {
            "end_period_ts": 1,
            "yes_bid": {"close_dollars": "0.0000"},
            "yes_ask": {"close_dollars": "1.0000"},
            "volume_fp": "0.00",
        }
    )
    assert fields["bid_close"] == 0.0
    assert fields["ask_close"] == 1.0
    assert not is_two_sided(fields["bid_close"], fields["ask_close"])


def test_bracket_daily_table_from_regression_markets() -> None:
    payload = json.loads((FIXTURES / "markets_2022-12-11.json").read_text(encoding="utf-8"))
    markets = payload["markets"]
    daily = build_daily_table(markets, start_date=date(2022, 12, 11))
    assert len(daily) == 1
    assert daily.iloc[0]["n_brackets"] == EXPECTED["bracket_structure"]["modern_n_brackets"]


def test_kelvin_to_fahrenheit_guard_on_typical_nbm_value() -> None:
    """Sanity: 300 K ≈ 80.33 °F within guard range."""
    temp_f = validate_temperature_f(k2f(300.0), label="NBM TMP")
    assert temp_f == pytest.approx(80.33, abs=0.01)
