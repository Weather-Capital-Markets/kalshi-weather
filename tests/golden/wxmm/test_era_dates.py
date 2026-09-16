"""Kalshi era effective_from dates are load-bearing venue facts."""

from __future__ import annotations

from datetime import date, datetime, timezone

from wxmm.settlement.eras import (
    KALSHI_LAST_TRADING_CIVIL_ET_THRU,
    KALSHI_RULES,
    POLYMARKET_RULES,
    kalshi_last_trading_close_utc,
)

UTC = timezone.utc


def test_kalshi_era_effective_from_dates_match_venue_facts() -> None:
    assert KALSHI_RULES[0].effective_from == date(2021, 8, 6)
    assert KALSHI_RULES[0].effective_to == date(2021, 12, 25)
    assert KALSHI_RULES[1].effective_from == date(2021, 12, 26)
    assert KALSHI_RULES[1].effective_to == date(2024, 9, 3)
    assert KALSHI_RULES[2].effective_from == date(2024, 9, 4)
    assert KALSHI_RULES[2].effective_to is None


def test_polymarket_era_effective_from_is_pinned() -> None:
    assert POLYMARKET_RULES[0].effective_from == date(2021, 1, 1)


def test_kalshi_last_trading_close_matches_venue_facts_section_1_8() -> None:
    assert KALSHI_LAST_TRADING_CIVIL_ET_THRU == date(2026, 3, 17)
    # 2026-03-16 is EDT: 11:59 PM civil ET = 03:59Z next day.
    assert kalshi_last_trading_close_utc(date(2026, 3, 16)) == datetime(
        2026, 3, 17, 3, 59, tzinfo=UTC
    )
    # From 2026-03-18: 11:59 PM LST = 04:59Z next day.
    assert kalshi_last_trading_close_utc(date(2026, 3, 18)) == datetime(
        2026, 3, 19, 4, 59, tzinfo=UTC
    )
