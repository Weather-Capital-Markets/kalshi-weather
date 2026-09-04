"""Risk limits. Hard stops; no silent override."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.money import Money


@dataclass(frozen=True, slots=True)
class Limits:
    max_notional: Money
    max_basis_risk_pct: Decimal
    max_contracts: int

    def check_notional(self, notional: Money) -> None:
        if abs(notional.amount) > self.max_notional.amount:
            raise ValueError(f"notional {notional} exceeds max {self.max_notional}")

    def check_contracts(self, quantity: int) -> None:
        if abs(quantity) > self.max_contracts:
            raise ValueError(f"quantity {quantity} exceeds max_contracts {self.max_contracts}")
