"""Hypothesis properties for Kalshi fees."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from wxmm.venues.kalshi.fees import FeeRounding, taker_fee


@given(price_cents=st.integers(1, 99), contracts=st.integers(1, 50))
@settings(max_examples=40)
def test_taker_fee_symmetric_in_p_and_nonnegative(price_cents: int, contracts: int) -> None:
    price = Decimal(price_cents) / Decimal(100)
    a = taker_fee(contracts, price, FeeRounding.CONTINUOUS)
    b = taker_fee(contracts, Decimal("1") - price, FeeRounding.CONTINUOUS)
    assert a == b
    assert a.amount >= 0
    cent = taker_fee(contracts, price, FeeRounding.PER_ORDER_CENT)
    assert cent.amount >= a.amount
