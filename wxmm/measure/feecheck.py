"""Predicted fee vs charged fee. Learn-mode until the schedule is verified.

Allowed to assume
    Kalshi ``round_up(0.07·C·P·(1−P))`` is documented — a mismatch raises.
    Polymarket ``rate`` 0.05 / ``rebateRate`` 0.25 stay learn-mode until
    ``POLYMARKET_FEE_SCHEDULE`` is verified.

Must never
    Assert Polymarket fees while unverified. Choose a plausible default.
    Enter verified-mode except through the ``VenueFact``.
"""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.core.errors import FeeMismatch, UnverifiedFactError
from wxmm.core.money import Money
from wxmm.core.types import POLYMARKET_FEE_SCHEDULE, FactStatus, Order, VenueFact
from wxmm.venues.kalshi.fees import FeeRounding, order_fee


@dataclass(frozen=True, slots=True)
class FeeCheckRecord:
    venue: str
    predicted: Money | None
    charged: Money
    mode: str
    matched: bool | None


def check_fill(
    order: Order,
    charged: Money,
    *,
    schedule: VenueFact | None = None,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
) -> FeeCheckRecord:
    if order.venue == "kalshi":
        predicted = order_fee(order, rounding)
        if predicted != charged:
            raise FeeMismatch(
                f"Kalshi fee mismatch predicted={predicted} charged={charged} "
                f"qty={order.quantity} px={order.price_cents}c taker={order.is_taker}"
            )
        return FeeCheckRecord(
            venue="kalshi",
            predicted=predicted,
            charged=charged,
            mode="verified",
            matched=True,
        )
    if order.venue == "polymarket":
        fact = schedule if schedule is not None else POLYMARKET_FEE_SCHEDULE
        if fact.status is FactStatus.VERIFIED:
            raise UnverifiedFactError(
                "Polymarket verified-mode is inexpressible until POLYMARKET_FEE_SCHEDULE "
                "is a VenueFact with status=verified and a date+source; refusing to "
                "assert a guessed formula"
            )
        return FeeCheckRecord(
            venue="polymarket",
            predicted=None,
            charged=charged,
            mode="learn",
            matched=None,
        )
    raise KeyError(f"no feecheck for venue {order.venue!r}")
