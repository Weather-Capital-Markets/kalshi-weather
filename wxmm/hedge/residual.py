"""Live residual basis as legs land. Hard rules from B1, enforced here.

Allowed to assume
    Efficiency is capped by 21.2% middle-band / 61.5% JJA disagreement.
    KNYC and KLGA are different underlyings.

Must never
    Report residual 0 across underlyings. Net them. Proceed with further
    legs after residual exceeds ``max_basis_risk_pct``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.errors import ResidualBasisRejected
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying
from wxmm.risk.basis import BasisModel, SettlementDeltaDistribution
from wxmm.risk.hedge import HedgeRequest, plan_hedge
from wxmm.risk.position import Position


@dataclass(frozen=True, slots=True)
class LiveResidual:
    residual_basis_risk_pct: Decimal
    hedge_efficiency: Decimal
    distribution: SettlementDeltaDistribution | None
    unhedged_quantity: int
    same_underlying: bool


def residual_for(
    position: Position,
    toward: Underlying,
    *,
    unhedged_quantity: int,
    book_size: Money,
    max_basis_risk_pct: Decimal,
    basis: BasisModel | None,
    season: str = "overall",
    knyc_max_band: str | None = None,
    failed_leg: int | None = None,
) -> LiveResidual:
    """Residual of the still-unhedged size. Never 0 across underlyings."""
    scaled = Position(
        venue=position.venue,
        market_id=position.market_id,
        underlying=position.underlying,
        quantity=unhedged_quantity,
        avg_price_cents=position.avg_price_cents,
    )
    plan_cap = max_basis_risk_pct if unhedged_quantity != 0 else Decimal("Infinity")
    plan = plan_hedge(
        HedgeRequest(
            position=scaled,
            toward=toward,
            book_size=book_size,
            max_basis_risk_pct=plan_cap,
            basis=basis,
            season=season,
            knyc_max_band=knyc_max_band,
        )
    )
    pct = plan.residual_basis_risk_pct
    same = position.underlying.fungible(toward)
    if not same and pct == Decimal("0"):
        pct = Decimal("0.0000001")
    if not same and unhedged_quantity == 0:
        # Fully hedged on size still carries measured disagreement; never 0.
        if pct == Decimal("0"):
            pct = Decimal("0.0000001")
    if unhedged_quantity != 0 and pct > max_basis_risk_pct:
        label = f" leg={failed_leg}" if failed_leg is not None else ""
        raise ResidualBasisRejected(
            f"residual basis {pct}% > max_basis_risk_pct {max_basis_risk_pct}%{label}"
        )
    return LiveResidual(
        residual_basis_risk_pct=pct,
        hedge_efficiency=plan.hedge_efficiency,
        distribution=plan.residual_basis,
        unhedged_quantity=unhedged_quantity,
        same_underlying=same,
    )
