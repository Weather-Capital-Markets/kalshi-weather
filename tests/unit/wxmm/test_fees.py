"""Kalshi fee rounding: per_order_cent vs continuous; six-leg basket."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wxmm.core.errors import UnverifiedFeeSchedule
from wxmm.core.money import Money
from wxmm.core.types import InMemoryAsOfStore, Order
from wxmm.venues.base import get_venue
from wxmm.venues.kalshi.fees import FeeRounding, basket_taker_fee, taker_fee


def test_per_order_cent_diverges_at_one_contract_converges_at_100() -> None:
    price = Decimal("0.50")
    one_cent = taker_fee(1, price, FeeRounding.PER_ORDER_CENT)
    one_cont = taker_fee(1, price, FeeRounding.CONTINUOUS)
    assert one_cent == Money.cents(2)
    assert one_cont == Money.dollars(Decimal("0.0175"))
    assert one_cent != one_cont

    hundred_cent = taker_fee(100, price, FeeRounding.PER_ORDER_CENT)
    hundred_cont = taker_fee(100, price, FeeRounding.CONTINUOUS)
    assert hundred_cent == hundred_cont
    assert hundred_cent == Money.dollars(Decimal("1.75"))


def test_six_leg_basket_both_rounding_policies() -> None:
    legs = (
        (1, Decimal("0.10")),
        (1, Decimal("0.20")),
        (1, Decimal("0.30")),
        (1, Decimal("0.40")),
        (1, Decimal("0.50")),
        (1, Decimal("0.60")),
    )
    per_order = basket_taker_fee(legs, FeeRounding.PER_ORDER_CENT)
    continuous = basket_taker_fee(legs, FeeRounding.CONTINUOUS)
    # Each 1-contract leg rounds up independently under per_order_cent.
    assert per_order != continuous
    assert per_order > continuous
    # Both ways: YES prices and the complementary NO prices 1-P must match
    # because the formula is P*(1-P).
    mirrored = tuple((c, Decimal("1") - p) for c, p in legs)
    assert basket_taker_fee(mirrored, FeeRounding.PER_ORDER_CENT) == per_order
    assert basket_taker_fee(mirrored, FeeRounding.CONTINUOUS) == continuous


def test_polymarket_fee_raises_unverified() -> None:
    venue = get_venue("polymarket", InMemoryAsOfStore())
    order = Order(
        venue="polymarket",
        market_id="nyc-76-77",
        side="buy",
        price_cents=40,
        quantity=1,
        is_taker=True,
    )
    with pytest.raises(UnverifiedFeeSchedule, match="Verify with date\\+source"):
        venue.fee(order)


def test_kalshi_maker_is_zero() -> None:
    venue = get_venue("kalshi", InMemoryAsOfStore())
    order = Order(
        venue="kalshi",
        market_id="KXHIGHNY-26JUL04-T90",
        side="buy",
        price_cents=40,
        quantity=10,
        is_taker=False,
    )
    assert venue.fee(order) == Money.zero()
