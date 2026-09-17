"""Basis is a distribution; cross-venue hedge keeps residual."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wxmm.core.errors import NonFungibleNettingError, ResidualBasisRejected
from wxmm.core.money import Money
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.risk.basis import BasisModel
from wxmm.risk.exposure import net_same_underlying
from wxmm.risk.hedge import HedgeRequest, plan_hedge
from wxmm.risk.position import Position


def test_basis_model_refuses_point_estimate() -> None:
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    dist = model.settlement_difference(season="JJA")
    assert dist.bracket_disagreement_rate == Decimal("0.615")
    assert dist.measured is True
    with pytest.raises(TypeError, match="never a point estimate"):
        dist.point_estimate()


def test_djf_disagreement_is_artifact_not_measurement() -> None:
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    dist = model.settlement_difference(season="DJF")
    assert dist.bracket_disagreement_rate == Decimal("0.000")
    assert dist.measured is False


def test_cannot_net_kalshi_against_polymarket() -> None:
    a = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 1, 40)
    b = Position("polymarket", "B", POLYMARKET_NYC_DAILY_HIGH, -1, 40)
    with pytest.raises(NonFungibleNettingError):
        net_same_underlying(a, b)


def test_cross_underlying_hedge_requires_basis_and_keeps_residual() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    rejected = plan_hedge(
        HedgeRequest(
            position=pos,
            toward=POLYMARKET_NYC_DAILY_HIGH,
            book_size=Money.cents(10000),
            max_basis_risk_pct=Decimal("100"),
            basis=None,
        )
    )
    assert rejected.rejected is True
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    plan = plan_hedge(
        HedgeRequest(
            position=pos,
            toward=POLYMARKET_NYC_DAILY_HIGH,
            book_size=Money.cents(10000),
            max_basis_risk_pct=Decimal("100"),
            basis=model,
            season="JJA",
        )
    )
    assert plan.rejected is False
    assert plan.residual_basis is not None
    assert plan.residual_basis_risk_pct > Decimal("0")
    assert plan.hedge_efficiency == Decimal("1") - Decimal("0.615")


def test_hedge_rejects_when_residual_exceeds_cap() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    with pytest.raises(ResidualBasisRejected):
        plan_hedge(
            HedgeRequest(
                position=pos,
                toward=POLYMARKET_NYC_DAILY_HIGH,
                book_size=Money.cents(10000),
                max_basis_risk_pct=Decimal("10"),
                basis=model,
                season="JJA",
            )
        )
