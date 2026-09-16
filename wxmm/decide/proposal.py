"""Immutable proposal. The human sends; this object never reaches a venue.

Allowed to assume
    ``rationale`` is mandatory. ``edge`` is None when no fair value was
    supplied. Polymarket modelled fee may be None (unverified).

Must never
    Default rationale. Import ``wxmm.execute``. Clip size to pass a limit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.money import Money


@dataclass(frozen=True, slots=True)
class Proposal:
    venue: str
    market: str
    side: str
    price: int
    size: int
    modelled_fee: Money | None
    modelled_collateral: Money
    edge: Decimal | None
    limit_checks_passed: tuple[str, ...]
    rate_limit_cost: float
    rationale: str
    rate_blocked: bool = False
    degradation: tuple[str, ...] = ()
    fee_unverified: bool = False

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"side must be buy|sell, not {self.side!r}")
        if not self.rationale.strip():
            raise ValueError("Proposal.rationale is required")
        if self.size <= 0:
            raise ValueError("Proposal.size must be positive")

    def content_hash(self) -> str:
        payload = {
            "venue": self.venue,
            "market": self.market,
            "side": self.side,
            "price": self.price,
            "size": self.size,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()
