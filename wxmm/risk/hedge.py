"""Hedge plans. Notionals as % of configured book size. Residual basis never zeroed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.errors import ResidualBasisRejected
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying
from wxmm.risk.basis import BasisModel, SettlementDeltaDistribution
from wxmm.risk.position import Position


@dataclass(frozen=True, slots=True)
class HedgeRequest:
    position: Position
    toward: Underlying
    book_size: Money
    max_basis_risk_pct: Decimal
    basis: BasisModel | None = None
    season: str = "overall"
    knyc_max_band: str | None = None


@dataclass(frozen=True, slots=True)
class HedgeLeg:
    venue: str
    market_id: str
    underlying: Underlying
    quantity: int
    notional_pct_of_book: Decimal


@dataclass(frozen=True, slots=True)
class HedgePlan:
    legs: tuple[HedgeLeg, ...]
    hedge_efficiency: Decimal
    residual_basis: SettlementDeltaDistribution | None
    residual_basis_risk_pct: Decimal
    rejected: bool
    reason: str | None

    def notional_pct_of_book(self) -> Decimal:
        return sum((leg.notional_pct_of_book for leg in self.legs), Decimal("0"))


def plan_hedge(request: HedgeRequest) -> HedgePlan:
    pos = request.position
    if pos.underlying.fungible(request.toward):
        qty = -pos.quantity
        notional = pos.notional()
        pct = Decimal("0")
        if notional is not None and request.book_size.amount != 0:
            pct = (abs(notional.amount) / request.book_size.amount) * Decimal(100)
        return HedgePlan(
            legs=(
                HedgeLeg(
                    venue=pos.venue,
                    market_id=pos.market_id,
                    underlying=pos.underlying,
                    quantity=qty,
                    notional_pct_of_book=pct,
                ),
            ),
            hedge_efficiency=Decimal("1"),
            residual_basis=None,
            residual_basis_risk_pct=Decimal("0"),
            rejected=False,
            reason="same_underlying_full_net",
        )

    if request.basis is None:
        return HedgePlan(
            legs=(),
            hedge_efficiency=Decimal("0"),
            residual_basis=None,
            residual_basis_risk_pct=Decimal("100"),
            rejected=True,
            reason="cross_underlying_requires_explicit_BasisModel",
        )

    dist = request.basis.settlement_difference(
        season=request.season,
        knyc_max_band=request.knyc_max_band,
    )
    efficiency = Decimal("1") - dist.bracket_disagreement_rate
    if efficiency < Decimal("0"):
        efficiency = Decimal("0")
    residual_pct = dist.bracket_disagreement_rate * Decimal(100)
    if residual_pct > request.max_basis_risk_pct:
        raise ResidualBasisRejected(
            f"residual basis {residual_pct}% > max_basis_risk_pct {request.max_basis_risk_pct}% "
            f"(regime={dist.regime} measured={dist.measured})"
        )
    if residual_pct == Decimal("0") and not pos.underlying.fungible(request.toward):
        # Never report residual basis as zero on a cross-underlying hedge.
        residual_pct = dist.bracket_disagreement_rate * Decimal(100)
        if residual_pct == Decimal("0"):
            # DJF artifact: still refuse to call residual zero.
            residual_pct = Decimal("0.0000001")
            dist = SettlementDeltaDistribution(
                n=dist.n,
                median_f=dist.median_f,
                share_klga_warmer=dist.share_klga_warmer,
                bracket_disagreement_rate=dist.bracket_disagreement_rate,
                regime=f"{dist.regime}_binning_artifact_residual_nonzero",
                measured=False,
                source=dist.source,
            )
    notional = pos.notional()
    pct = Decimal("0")
    if notional is not None and request.book_size.amount != 0:
        pct = (abs(notional.amount) / request.book_size.amount) * Decimal(100) * efficiency
    return HedgePlan(
        legs=(
            HedgeLeg(
                venue="hedge",
                market_id=f"basis:{request.toward.station}",
                underlying=request.toward,
                quantity=-pos.quantity,
                notional_pct_of_book=pct,
            ),
        ),
        hedge_efficiency=efficiency,
        residual_basis=dist,
        residual_basis_risk_pct=residual_pct,
        rejected=False,
        reason=None,
    )
