"""Held-to-expiry settlement inside the backtest. Fee schedule is (verify)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from wxmm.core.errors import UnverifiedSettlementFee
from wxmm.core.money import Money
from wxmm.core.types import Order
from wxmm.core.utc import require_utc
from wxmm.settlement.eras import polymarket_rule_for
from wxmm.settlement.resolvers import kalshi as kalshi_resolver
from wxmm.settlement.resolvers import polymarket as pm_resolver
from wxmm.settlement.rules import Observation, SettlementResult
from wxmm.venues.kalshi.fees import FeeRounding, order_fee


@dataclass(frozen=True, slots=True)
class SettlementFeePolicy:
    """Kalshi fee on settled (held-to-expiry) contracts is (verify).

    Default ``unverified`` raises rather than pricing. Hold-to-expiry is the
    natural manual-operator mode and cannot be implicit.
    """

    name: str = "unverified"

    def charge(self, order: Order, rounding: FeeRounding = FeeRounding.PER_ORDER_CENT) -> Money:
        if self.name == "unverified":
            raise UnverifiedSettlementFee(
                "Kalshi fee on settled (held-to-expiry) contracts is (verify); "
                "settlement_fee_policy=unverified will not price hold-to-expiry P&L"
            )
        if self.name == "kalshi_taker_as_if_fill":
            return order_fee(order, rounding)
        raise UnverifiedSettlementFee(
            f"settlement_fee_policy={self.name!r} is not a verified schedule"
        )


@dataclass(frozen=True, slots=True)
class ResolutionPath:
    climate_day: date
    venue: str
    initial_high_f: int | None
    final_high_f: int | None
    flipped: bool
    initial_as_of: datetime
    final_as_of: datetime


def resolve_at(
    venue: str,
    climate_day: date,
    observations: Sequence[Observation],
    as_of: datetime,
) -> SettlementResult:
    obs = tuple(observations)
    if venue == "kalshi":
        return kalshi_resolver.resolve(climate_day, obs, as_of)
    if venue == "polymarket":
        return pm_resolver.resolve(climate_day, obs, as_of)
    raise KeyError(f"unknown venue {venue!r}")


def binary_payout_cents(high_f: int | None, *, yes_if_at_least: int) -> int | None:
    """YES=100 / NO=0. Missing high_f is not zero."""
    if high_f is None:
        return None
    return 100 if high_f >= yes_if_at_least else 0


def polymarket_resolution_path(
    climate_day: date,
    observations: Sequence[Observation],
    *,
    as_of_final: datetime,
) -> ResolutionPath:
    """First published high vs high accepted at ``as_of_final``.

    ``accept_until_next_first_datapoint`` can change a position's resolution
    after the first datapoint; the backtest reports days they differ.
    """
    as_of_final_utc = require_utc(as_of_final)
    station = polymarket_rule_for(climate_day).underlying.station
    published = [
        obs.available_at
        for obs in observations
        if obs.climate_day == climate_day
        and obs.station == station
        and obs.available_at <= as_of_final_utc
    ]
    as_of_initial = min(published) if published else as_of_final_utc
    obs = tuple(observations)
    initial = pm_resolver.resolve(climate_day, obs, as_of_initial)
    final = pm_resolver.resolve(climate_day, obs, as_of_final_utc)
    return ResolutionPath(
        climate_day=climate_day,
        venue="polymarket",
        initial_high_f=initial.high_f,
        final_high_f=final.high_f,
        flipped=initial.high_f != final.high_f,
        initial_as_of=as_of_initial,
        final_as_of=as_of_final_utc,
    )


def hold_to_expiry_pnl(
    *,
    quantity: int,
    entry_cents: int,
    settle_cents: int,
    policy: SettlementFeePolicy,
    venue: str,
    market_id: str,
) -> Money:
    """Mark held inventory to 0/1 and apply settlement fee. Unverified raises."""
    dummy = Order(
        venue=venue,
        market_id=market_id,
        side="buy",
        price_cents=entry_cents,
        quantity=abs(quantity),
        is_taker=False,
    )
    fee = policy.charge(dummy)
    raw = Money.cents((settle_cents - entry_cents) * quantity)
    return raw - fee
