"""Bracket ladder parsing, continuity correction, normalisation goldens."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from wxmm.fairvalue.ladder import (
    assert_normalised,
    ladder_order,
    parse_kalshi_bracket,
    renormalise_clipped,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "regression" / "markets_2022-12-11.json"
)


def test_parse_tail_and_between_half() -> None:
    tail = parse_kalshi_bracket("KXHIGHNY-26JUL04-T86")
    assert tail is not None
    assert tail.role == "greater"
    assert tail.continuity_bounds_f() == (85.5, None)

    between = parse_kalshi_bracket("KXHIGHNY-26JUL04-B84.5")
    assert between is not None
    assert between.role == "between"
    assert between.continuity_bounds_f() == (83.5, 85.5)


def test_continuity_edges_int_between() -> None:
    row = parse_kalshi_bracket("KXHIGHNY-26JUL04-B85")
    assert row is not None
    assert row.continuity_bounds_f() == (84.5, 85.5)


def test_renormalise_clipped_sums_to_one() -> None:
    raw = {"a": Decimal("0.6"), "b": Decimal("0.5")}
    out = renormalise_clipped(raw)
    assert_normalised(out)


def test_assert_normalised_rejects_bad_mass() -> None:
    with pytest.raises(AssertionError):
        assert_normalised({"a": Decimal("0.4"), "b": Decimal("0.4")})


def _metadata_order(markets: list[dict[str, object]]) -> list[str]:
    less = [row for row in markets if row["strike_type"] == "less"]
    between = [row for row in markets if row["strike_type"] == "between"]
    greater = [row for row in markets if row["strike_type"] == "greater"]
    less.sort(key=lambda row: int(row["cap_strike"]))  # type: ignore[arg-type]
    between.sort(key=lambda row: (int(row["floor_strike"]), int(row["cap_strike"])))  # type: ignore[arg-type]
    greater.sort(key=lambda row: int(row["floor_strike"]))  # type: ignore[arg-type]
    return [str(row["ticker"]) for row in [*less, *between, *greater]]


def test_ladder_order_follows_strike_metadata() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    markets = payload["markets"]
    assert isinstance(markets, list)
    expected = _metadata_order(markets)
    tickers = [str(row["ticker"]) for row in markets]
    ordered = ladder_order(tickers)
    assert ordered == expected
    assert ordered != sorted(tickers)
    assert ordered[0].endswith("-T83")
    assert ordered[-1].endswith("-T90")


def test_ladder_order_uses_thresholds_past_ten_brackets() -> None:
    betweens = [f"KXHIGHNY-26JUL04-B{level}.5" for level in range(71, 95, 2)]
    tickers = ["KXHIGHNY-26JUL04-T70", *betweens, "KXHIGHNY-26JUL04-T94"]
    ordered = ladder_order(tickers)
    assert ordered[0].endswith("-T70")
    assert ordered[-1].endswith("-T94")
    assert ordered != sorted(tickers)
    wide = ladder_order(["KXHIGHNY-26JUL04-B100.5", "KXHIGHNY-26JUL04-B96.5"])
    assert wide == ["KXHIGHNY-26JUL04-B96.5", "KXHIGHNY-26JUL04-B100.5"]
    assert wide != sorted(wide)


def test_two_tails_lower_threshold_is_below() -> None:
    low, high = "KXHIGHNY-26JUL04-T90", "KXHIGHNY-26JUL04-T100"
    assert ladder_order([high, low]) == [low, high]
    assert ladder_order([high, low]) != sorted([high, low])


def test_interior_tail_is_refused() -> None:
    tickers = [
        "KXHIGHNY-26JUL04-T86",
        "KXHIGHNY-26JUL04-B83.5",
        "KXHIGHNY-26JUL04-B89.5",
    ]
    with pytest.raises(ValueError, match="cannot orient"):
        ladder_order(tickers)
