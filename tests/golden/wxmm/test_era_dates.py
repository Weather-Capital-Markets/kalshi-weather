"""Kalshi era effective_from dates are load-bearing venue facts."""

from __future__ import annotations

from datetime import date

from wxmm.settlement.eras import KALSHI_RULES, POLYMARKET_RULES


def test_kalshi_era_effective_from_dates_match_venue_facts() -> None:
    assert KALSHI_RULES[0].effective_from == date(2021, 8, 6)
    assert KALSHI_RULES[0].effective_to == date(2021, 12, 25)
    assert KALSHI_RULES[1].effective_from == date(2021, 12, 26)
    assert KALSHI_RULES[1].effective_to == date(2024, 9, 3)
    assert KALSHI_RULES[2].effective_from == date(2024, 9, 4)
    assert KALSHI_RULES[2].effective_to is None


def test_polymarket_era_effective_from_is_pinned() -> None:
    assert POLYMARKET_RULES[0].effective_from == date(2021, 1, 1)
