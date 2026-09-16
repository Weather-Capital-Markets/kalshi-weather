"""Pre-trade limits. Config-driven. Breach names the limit and the overrun.

Allowed to assume
    Limits are part of backtest/console config: max notional per climate day,
    max collateral committed, max exposure per Underlying, max residual basis.

Must never
    Silently clip a proposal. Call wall-clock. Sum exposure across
    Underlyings to compare to a single cap without naming which Underlying.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.errors import LimitBreach
from wxmm.core.money import Money
from wxmm.core.underlying import Underlying
from wxmm.risk.exposure import UnderlyingExposure
from wxmm.strategy.view import ProposedOrder


@dataclass(frozen=True, slots=True)
class Limits:
    max_notional_per_climate_day: Money
    max_collateral_committed: Money
    max_exposure_per_underlying: Money
    max_residual_basis_pct: Decimal

    def check_notional_per_climate_day(self, notional: Money) -> None:
        self._money("max_notional_per_climate_day", notional, self.max_notional_per_climate_day)

    def check_collateral(self, collateral: Money) -> None:
        self._money("max_collateral_committed", collateral, self.max_collateral_committed)

    def check_exposure_per_underlying(self, exposure: UnderlyingExposure) -> None:
        gross = Money(abs(exposure.notional.amount))
        try:
            self._money("max_exposure_per_underlying", gross, self.max_exposure_per_underlying)
        except LimitBreach as exc:
            raise LimitBreach(
                "max_exposure_per_underlying",
                actual=f"{gross} underlying={exposure.underlying.station}",
                allowed=str(self.max_exposure_per_underlying),
                overrun=exc.overrun,
            ) from exc

    def check_residual_basis_pct(self, residual_pct: Decimal) -> None:
        if residual_pct > self.max_residual_basis_pct:
            overrun = residual_pct - self.max_residual_basis_pct
            raise LimitBreach(
                "max_residual_basis",
                actual=f"{residual_pct}%",
                allowed=f"{self.max_residual_basis_pct}%",
                overrun=f"{overrun}%",
            )

    def check_proposal(
        self,
        proposal: ProposedOrder,
        *,
        climate_day_notional: Money,
        collateral: Money,
        exposure: UnderlyingExposure | None,
        residual_basis_pct: Decimal,
        underlying: Underlying | None = None,
    ) -> None:
        """Run all pre-trade checks. First breach raises with the limit name."""
        proposed_notional = Money.cents(proposal.price * proposal.size)
        self.check_notional_per_climate_day(climate_day_notional + proposed_notional)
        self.check_collateral(collateral + proposed_notional)
        if exposure is not None:
            self.check_exposure_per_underlying(exposure)
        self.check_residual_basis_pct(residual_basis_pct)
        _ = underlying

    def _money(self, name: str, actual: Money, allowed: Money) -> None:
        if abs(actual.amount) > allowed.amount:
            overrun = Money(abs(actual.amount) - allowed.amount)
            raise LimitBreach(
                name,
                actual=str(actual),
                allowed=str(allowed),
                overrun=str(overrun),
            )
