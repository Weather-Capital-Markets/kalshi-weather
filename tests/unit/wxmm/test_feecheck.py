"""feecheck: Kalshi mismatch raises; Polymarket learn-mode records only."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wxmm.core.errors import FeeMismatch, UnverifiedFactError
from wxmm.core.money import Money
from wxmm.core.types import FactStatus, Order, VenueFact
from wxmm.measure.feecheck import check_fill
from wxmm.venues.kalshi.fees import order_fee

UTC = timezone.utc


def _kalshi(qty: int, px: int, *, taker: bool) -> Order:
    return Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=px,
        quantity=qty,
        is_taker=taker,
    )


def test_kalshi_learn_not_needed_match_and_mismatch() -> None:
    order = _kalshi(1, 50, taker=True)
    predicted = order_fee(order)
    rec = check_fill(order, predicted)
    assert rec.mode == "verified"
    assert rec.matched is True
    with pytest.raises(FeeMismatch):
        check_fill(order, Money.cents(99))


def test_kalshi_one_and_hundred_contracts_both_granularities() -> None:
    for qty in (1, 100):
        order = _kalshi(qty, 50, taker=True)
        rec = check_fill(order, order_fee(order))
        assert rec.matched is True


def test_polymarket_learn_mode_records_without_asserting() -> None:
    order = Order(
        venue="polymarket",
        market_id="nyc-76-77",
        side="buy",
        price_cents=40,
        quantity=1,
        is_taker=True,
    )
    rec = check_fill(order, Money.cents(7))
    assert rec.mode == "learn"
    assert rec.matched is None
    assert rec.predicted is None
    rec2 = check_fill(order, Money.cents(1))
    assert rec2.mode == "learn"


def test_polymarket_verified_mode_inexpressible() -> None:
    order = Order(
        venue="polymarket",
        market_id="nyc-76-77",
        side="buy",
        price_cents=40,
        quantity=1,
        is_taker=True,
    )
    fake_verified = VenueFact(
        name="polymarket_nyc_fee_schedule",
        status=FactStatus.VERIFIED,
        source="test",
        verified_on=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(UnverifiedFactError):
        check_fill(order, Money.cents(1), schedule=fake_verified)
