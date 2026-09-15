"""Bracket ladder parsing, continuity correction, normalisation goldens."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wxmm.fairvalue.ladder import (
    assert_normalised,
    parse_kalshi_bracket,
    renormalise_clipped,
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
