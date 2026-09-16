"""Hand-executed multi-leg hedge. PARTIAL is the normal dangerous state.

Allowed to assume
    Legs are sent by a human through ``SendGate``. Basis is re-evaluated
    before each subsequent leg.

Must never
    Auto-send the next leg. Continue after residual cap breach. Hide
    PARTIAL exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from wxmm.core.errors import ResidualBasisRejected
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying
from wxmm.decide.proposal import Proposal
from wxmm.execute.send_gate import ConfirmationToken, SendGate, SendResult
from wxmm.hedge.planner import ConcreteLeg, ExecutableHedge, plan_executable
from wxmm.hedge.residual import LiveResidual, residual_for
from wxmm.risk.basis import BasisModel
from wxmm.risk.hedge import HedgeRequest
from wxmm.risk.position import Position


class HedgeState(str, Enum):
    UNHEDGED = "UNHEDGED"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    ABANDONED = "ABANDONED"


@dataclass
class HedgeExecutor:
    position: Position
    toward: Underlying
    book_size: Money
    max_basis_risk_pct: Decimal
    basis: BasisModel | None
    season: str = "overall"
    knyc_max_band: str | None = None
    state: HedgeState = HedgeState.UNHEDGED
    filled_qty_hedge: int = 0
    open_exposure: int = 0
    last_residual: LiveResidual | None = None
    stopped_at_leg: int | None = None
    _legs: tuple[ConcreteLeg, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.open_exposure = self.position.quantity

    def plan_now(
        self,
        *,
        at: datetime,
        hedge_venue: str,
        hedge_market_id: str,
    ) -> ExecutableHedge:
        planned = plan_executable(
            HedgeRequest(
                position=self.position,
                toward=self.toward,
                book_size=self.book_size,
                max_basis_risk_pct=self.max_basis_risk_pct,
                basis=self.basis,
                season=self.season,
                knyc_max_band=self.knyc_max_band,
            ),
            at=at,
            hedge_venue=hedge_venue,
            hedge_market_id=hedge_market_id,
        )
        self._legs = planned.legs
        return planned

    def note_leg_fill(self, leg_index: int, filled_qty: int) -> LiveResidual:
        self.filled_qty_hedge += filled_qty
        remaining = abs(self.position.quantity) - abs(self.filled_qty_hedge)
        if remaining < 0:
            remaining = 0
        self.open_exposure = remaining if self.position.quantity >= 0 else -remaining
        if remaining == 0:
            self.state = HedgeState.COMPLETE
        else:
            self.state = HedgeState.PARTIAL
        try:
            res = residual_for(
                self.position,
                self.toward,
                unhedged_quantity=self.open_exposure,
                book_size=self.book_size,
                max_basis_risk_pct=self.max_basis_risk_pct,
                basis=self.basis,
                season=self.season,
                knyc_max_band=self.knyc_max_band,
                failed_leg=leg_index,
            )
        except ResidualBasisRejected:
            self.stopped_at_leg = leg_index
            self.state = HedgeState.PARTIAL
            raise
        self.last_residual = res
        return res

    def note_leg_reject(self, leg_index: int) -> None:
        self.state = HedgeState.PARTIAL
        self.stopped_at_leg = leg_index

    def abandon(self) -> None:
        self.state = HedgeState.ABANDONED

    def can_propose_next_leg(self) -> bool:
        if self.state in {HedgeState.COMPLETE, HedgeState.ABANDONED}:
            return False
        if self.stopped_at_leg is not None:
            return False
        if (
            self.last_residual is not None
            and self.last_residual.residual_basis_risk_pct > self.max_basis_risk_pct
        ):
            return False
        return True

    def send_leg(
        self,
        gate: SendGate,
        proposal: Proposal,
        token: ConfirmationToken,
        *,
        intent_id: str,
    ) -> SendResult:
        """Human-gated send. Token is required; there is no auto-send."""
        if not self.can_propose_next_leg():
            raise ResidualBasisRejected(
                f"cannot send hedge leg; state={self.state.value} "
                f"stopped_at={self.stopped_at_leg}"
            )
        return gate.send(proposal, token, intent_id=intent_id)
