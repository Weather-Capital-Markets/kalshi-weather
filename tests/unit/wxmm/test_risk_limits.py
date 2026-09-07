"""Positions, exposure, and pre-trade limits."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wxmm.core.errors import LimitBreach, NonFungibleNettingError
from wxmm.core.money import Money
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.risk.exposure import by_underlying, sum_notional_across
from wxmm.risk.limits import Limits
from wxmm.risk.position import Position, net_quantity
from wxmm.strategy.view import ProposedOrder


def test_position_aggregation_across_non_fungible_raises() -> None:
    a = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 1, 40)
    b = Position("polymarket", "B", POLYMARKET_NYC_DAILY_HIGH, 1, 40)
    with pytest.raises(NonFungibleNettingError, match="not fungible"):
        net_quantity([a, b])


def test_exposure_not_summed_across_underlyings() -> None:
    positions = (
        Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 2, 40),
        Position("polymarket", "B", POLYMARKET_NYC_DAILY_HIGH, 3, 50),
    )
    rows = by_underlying(positions)
    assert len(rows) == 2
    with pytest.raises(NonFungibleNettingError):
        sum_notional_across(rows)


def test_limit_breach_message_names_the_limit() -> None:
    limits = Limits(
        max_notional_per_climate_day=Money.cents(100),
        max_collateral_committed=Money.cents(10_000),
        max_exposure_per_underlying=Money.cents(10_000),
        max_residual_basis_pct=Decimal("50"),
    )
    proposal = ProposedOrder(
        venue="kalshi",
        market="KXHIGHNY-26JUL04-T90",
        side="buy",
        price=50,
        size=10,
        rationale="unit test",
    )
    with pytest.raises(LimitBreach, match="max_notional_per_climate_day") as exc_info:
        limits.check_proposal(
            proposal,
            climate_day_notional=Money.zero(),
            collateral=Money.zero(),
            exposure=None,
            residual_basis_pct=Decimal("0"),
        )
    assert exc_info.value.limit_name == "max_notional_per_climate_day"
    assert "by" in str(exc_info.value)
