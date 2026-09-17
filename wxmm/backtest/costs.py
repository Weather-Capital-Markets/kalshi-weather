"""Fill costs and collateral. Polymarket fees are a hard failure until verified."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.errors import UnverifiedFactError, UnverifiedFeeSchedule
from wxmm.core.money import Money
from wxmm.core.types import Order
from wxmm.venues.base import Venue
from wxmm.venues.kalshi.fees import FeeRounding, order_fee


@dataclass(frozen=True, slots=True)
class CollateralPolicy:
    """negRisk / multi-bracket collateral is UNVERIFIED. Default: no_netting."""

    mode: str = "no_netting"

    def __post_init__(self) -> None:
        if self.mode not in {"no_netting", "neg_risk"}:
            raise ValueError(f"unknown collateral mode {self.mode!r}")

    def require_netting_allowed(self) -> None:
        if self.mode == "neg_risk":
            raise UnverifiedFactError(
                "Polymarket negRisk / multi-bracket collateral UNVERIFIED; "
                "CollateralPolicy.neg_risk is a stub. Default remains no_netting."
            )


def fill_cost(
    venue: Venue,
    order: Order,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
) -> Money:
    if venue.name == "polymarket":
        raise UnverifiedFeeSchedule(
            "backtest/costs refuses to price a Polymarket fill while the fee "
            "schedule is unverified (date+source required)"
        )
    if venue.name == "kalshi":
        return order_fee(order, rounding)
    return venue.fee(order)


def contract_notional(quantity: int, price_cents: int) -> Money:
    return Money.cents(quantity * price_cents)


def no_netting_collateral(notionals: tuple[Money, ...]) -> Money:
    """Sum of absolute notionals. Never nets Kalshi against Polymarket."""
    total = Money.zero()
    for item in notionals:
        amount = item.amount if item.amount >= Decimal("0") else -item.amount
        total = total + Money(amount)
    return total
