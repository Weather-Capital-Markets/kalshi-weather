"""Tests for Kalshi bracket structure enumeration."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.bracket_enumeration import (
    build_daily_table,
    build_regime_table,
    contiguous_interior,
    event_structure,
    parse_market_strike,
    parse_strike_from_subtitle,
    verdict_on_two_degree_hypothesis,
)
from ingestion.writer import RawJsonlWriter, utc_now_iso


def _write_markets(raw_dir: Path, key: str, markets: list[dict]) -> None:
    writer = RawJsonlWriter(raw_dir)
    writer.write(
        ts_utc=utc_now_iso(),
        endpoint="/historical/markets",
        category="markets_history",
        key=key,
        http_status=200,
        latency_ms=1,
        payload={"markets": markets},
    )
    writer.close()


def _modern_event(prefix: str, *, width: int = 2) -> list[dict]:
    """Six-market ladder: less tail, four interior bins, greater tail."""
    if width == 2:
        interiors = [
            ("B83.5", "between", 83, 84),
            ("B85.5", "between", 85, 86),
            ("B87.5", "between", 87, 88),
            ("B89.5", "between", 89, 90),
        ]
    else:
        interiors = [
            ("B83.5", "between", 83, 83),
            ("B84.5", "between", 84, 84),
            ("B85.5", "between", 85, 85),
            ("B86.5", "between", 86, 86),
        ]
    markets = [
        {
            "ticker": f"{prefix}-T83",
            "strike_type": "less",
            "cap_strike": 83,
            "yes_sub_title": "82° or below",
        },
        {
            "ticker": f"{prefix}-T90",
            "strike_type": "greater",
            "floor_strike": 90,
            "yes_sub_title": "91° or above",
        },
    ]
    for suffix, strike_type, low, high in interiors:
        markets.append(
            {
                "ticker": f"{prefix}-{suffix}",
                "strike_type": strike_type,
                "floor_strike": low,
                "cap_strike": high,
                "yes_sub_title": f"{low}° to {high}°",
            }
        )
    return markets


def test_parse_strike_from_subtitle_between_and_tails() -> None:
    between = parse_strike_from_subtitle("87° to 88°")
    assert between is not None
    assert between.role == "between"
    assert between.width_f == 2
    above = parse_strike_from_subtitle("91° or above")
    assert above is not None and above.role == "greater"
    below = parse_strike_from_subtitle("82° or below")
    assert below is not None and below.role == "less"


def test_parse_market_strike_prefers_metadata() -> None:
    strike = parse_market_strike(
        {
            "strike_type": "between",
            "floor_strike": 87,
            "cap_strike": 88,
            "yes_sub_title": "wrong label",
        }
    )
    assert strike is not None
    assert strike.strike_source == "metadata"
    assert strike.width_f == 2


def test_contiguous_interior_detects_gap() -> None:
    assert contiguous_interior([(83, 84), (85, 86)])
    assert not contiguous_interior([(83, 84), (87, 88)])


def test_event_structure_modern_ladder() -> None:
    markets = _modern_event("KXHIGHNY-22JUL04")
    row = event_structure("2022-07-04", markets)
    assert row is not None
    assert row["n_brackets"] == 6
    assert row["interior_width_f"] == 2
    assert row["open_tails"] is True
    assert row["contiguous_interior"] is True
    assert "integer_2f_bins" in row["boundary_alignment"]


def test_regime_change_on_width_transition(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    _write_markets(raw_dir, "a", _modern_event("KXHIGHNY-22JUL04", width=2))
    _write_markets(raw_dir, "b", _modern_event("KXHIGHNY-22JUL05", width=1))

    from analysis.spread_census import load_markets

    markets = load_markets(raw_dir)
    daily = build_daily_table(markets, start_date=__import__("datetime").date(2022, 7, 4))
    regimes = build_regime_table(daily)
    assert len(regimes) == 2
    assert regimes.iloc[0]["interior_width_f"] == 2
    assert regimes.iloc[1]["interior_width_f"] == 1
    assert regimes.iloc[0]["last_date"] == "2022-07-04"
    assert regimes.iloc[1]["first_date"] == "2022-07-05"


def test_verdict_flags_era_dependent() -> None:
    daily = pd.DataFrame(
        [
            {"climate_date": "2022-07-04", "interior_width_f": 2, "structure_id": "a"},
            {"climate_date": "2022-07-05", "interior_width_f": 1, "structure_id": "b"},
        ]
    )
    regimes = build_regime_table(daily)
    assert verdict_on_two_degree_hypothesis(regimes, daily) == "era_dependent"
