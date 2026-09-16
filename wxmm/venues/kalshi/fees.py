"""Kalshi taker/maker fees. Decimal only.

Taker: round_up(0.07 · C · P · (1−P)) with rounding policy
``per_order_cent`` (default) or ``continuous``.

Maker: $0.00 verified 2026-08-15 (venue-facts §2.1). Capital use must still
pass ``KALSHI_WEATHER_MAKER_FEE.require_for_capital``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from wxmm.core.money import Money
from wxmm.core.types import Order

TAKER_COEFF = Decimal("0.07")


class FeeRounding(str, Enum):
    PER_ORDER_CENT = "per_order_cent"
    CONTINUOUS = "continuous"


def taker_fee(
    contracts: int,
    yes_price: Decimal,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
) -> Money:
    if contracts < 0:
        raise ValueError("contracts must be >= 0")
    if yes_price < Decimal("0") or yes_price > Decimal("1"):
        raise ValueError("yes_price must be in [0, 1]")
    raw = TAKER_COEFF * Decimal(contracts) * yes_price * (Decimal("1") - yes_price)
    money = Money(raw)
    if rounding is FeeRounding.PER_ORDER_CENT:
        return money.round_up_to_cent()
    return money


def maker_fee(_contracts: int, _yes_price: Decimal) -> Money:
    return Money.zero()


def order_fee(order: Order, rounding: FeeRounding = FeeRounding.PER_ORDER_CENT) -> Money:
    price = Decimal(order.price_cents) / Decimal(100)
    if order.is_taker:
        return taker_fee(order.quantity, price, rounding)
    return maker_fee(order.quantity, price)


def basket_taker_fee(
    legs: tuple[tuple[int, Decimal], ...] | list[tuple[int, Decimal]],
    rounding: FeeRounding,
) -> Money:
    total = Money.zero()
    for contracts, price in legs:
        total = total + taker_fee(contracts, price, rounding)
    return total
