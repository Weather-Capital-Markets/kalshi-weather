"""Kalshi NYC and Polymarket NYC are not fungible."""

from __future__ import annotations

from wxmm.core.underlying import (
    KALSHI_NYC_DAILY_HIGH,
    POLYMARKET_NYC_DAILY_HIGH,
    Underlying,
    UnderlyingRegistry,
)


def test_same_four_fields_are_fungible() -> None:
    a = Underlying("KNYC", "daily_high", "nws_cli_lst", "ignore_after_snapshot")
    b = Underlying("KNYC", "daily_high", "nws_cli_lst", "ignore_after_snapshot")
    assert a.fungible(b)
    assert hash(a) == hash(b)


def test_kalshi_and_polymarket_nyc_are_different_underlyings() -> None:
    assert not KALSHI_NYC_DAILY_HIGH.fungible(POLYMARKET_NYC_DAILY_HIGH)
    assert KALSHI_NYC_DAILY_HIGH.station == "KNYC"
    assert POLYMARKET_NYC_DAILY_HIGH.station == "KLGA"
    assert KALSHI_NYC_DAILY_HIGH.revision_rule != POLYMARKET_NYC_DAILY_HIGH.revision_rule
    assert KALSHI_NYC_DAILY_HIGH.day_convention != POLYMARKET_NYC_DAILY_HIGH.day_convention


def test_registry_maps_venue_market_to_underlying() -> None:
    registry = UnderlyingRegistry()
    registry.default_nyc()
    assert registry.get("kalshi", "KXHIGHNY") == KALSHI_NYC_DAILY_HIGH
    assert registry.get("polymarket", "nyc-daily-weather") == POLYMARKET_NYC_DAILY_HIGH
