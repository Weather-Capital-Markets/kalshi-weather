"""Kalshi NYC and Polymarket NYC are not fungible."""

from __future__ import annotations

import pytest

from wxmm.core.errors import NonFungibleNettingError
from wxmm.core.underlying import (
    DAY_CONVENTION_WU_UNVERIFIED,
    KALSHI_NYC_DAILY_HIGH,
    POLYMARKET_NYC_DAILY_HIGH,
    Underlying,
    UnderlyingRegistry,
)
from wxmm.risk.position import Position, net_quantity


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


def test_polymarket_day_convention_is_unverified_and_not_fungible_with_self() -> None:
    assert POLYMARKET_NYC_DAILY_HIGH.day_convention == DAY_CONVENTION_WU_UNVERIFIED
    assert POLYMARKET_NYC_DAILY_HIGH.day_convention.startswith("UNVERIFIED:")
    a = POLYMARKET_NYC_DAILY_HIGH
    b = Underlying(
        station="KLGA",
        product="daily_high",
        day_convention=DAY_CONVENTION_WU_UNVERIFIED,
        revision_rule="accept_until_next_first_datapoint",
    )
    assert not a.fungible(a)
    assert not a.fungible(b)
    p1 = Position("polymarket", "m1", a, 1, 40)
    p2 = Position("polymarket", "m2", b, -1, 40)
    with pytest.raises(NonFungibleNettingError):
        net_quantity([p1, p2])
