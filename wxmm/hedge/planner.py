"""Turn a HedgePlan into send-ordered concrete legs.

Allowed to assume
    ``plan_hedge`` is called at execution time, not reused from an earlier
    plan. Cross-underlying hedges require an explicit BasisModel.

Must never
    Net different Underlyings. Invent a hedge market id. Skip the send gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wxmm.core.underlying import Underlying
from wxmm.risk.hedge import HedgeLeg, HedgePlan, HedgeRequest, plan_hedge


@dataclass(frozen=True, slots=True)
class ConcreteLeg:
    send_order: int
    venue: str
    market_id: str
    underlying: Underlying
    quantity: int
    source_leg: HedgeLeg


@dataclass(frozen=True, slots=True)
class ExecutableHedge:
    plan: HedgePlan
    legs: tuple[ConcreteLeg, ...]
    planned_at: datetime


def plan_executable(
    request: HedgeRequest,
    *,
    at: datetime,
    hedge_venue: str,
    hedge_market_id: str,
) -> ExecutableHedge:
    """Re-evaluate ``plan_hedge`` at ``at``. Replace the placeholder hedge lot."""
    _ = at
    plan = plan_hedge(request)
    legs: list[ConcreteLeg] = []
    for i, leg in enumerate(plan.legs):
        venue = hedge_venue if leg.venue == "hedge" else leg.venue
        market_id = hedge_market_id if leg.venue == "hedge" else leg.market_id
        legs.append(
            ConcreteLeg(
                send_order=i,
                venue=venue,
                market_id=market_id,
                underlying=leg.underlying,
                quantity=leg.quantity,
                source_leg=leg,
            )
        )
    return ExecutableHedge(plan=plan, legs=tuple(legs), planned_at=at)
