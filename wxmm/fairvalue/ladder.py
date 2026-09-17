"""Bracket ladder mapping and normalisation for KXHIGHNY.

Allowed to assume
    Bracket tickers follow observed KXHIGHNY suffix conventions (T tail, B
    between). Continuity correction is a tagged assumption pending NWSI read.

Must never
    Hard-code a single bracket-width convention across eras. Return a partial
    ladder when any bracket quote is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from wxmm.core.errors import TaggedAssumption

TAIL_ABOVE = re.compile(r"-T(\d+)$", re.IGNORECASE)
BETWEEN_HALF = re.compile(r"-B(\d+)\.5$", re.IGNORECASE)
BETWEEN_INT = re.compile(r"-B(\d+)$", re.IGNORECASE)

CONTINUITY_CORRECTION = TaggedAssumption(
    "half_degree_f",
    assumption_id="cli_whole_f_half_degree",
    note=(
        "CLI reports whole °F (verify: implied by vendor example, not NWSI 10-1004). "
        "Bracket [a,b] maps to [a−0.5, b+0.5)."
    ),
)


@dataclass(frozen=True, slots=True)
class KalshiBracket:
    market_id: str
    role: str
    floor_f: int | None
    cap_f: int | None

    def continuity_bounds_f(self) -> tuple[float | None, float | None]:
        """Temperature bounds in °F after half-degree correction."""
        if self.role == "between" and self.floor_f is not None and self.cap_f is not None:
            return self.floor_f - 0.5, self.cap_f + 0.5
        if self.role == "greater" and self.floor_f is not None:
            return self.floor_f - 0.5, None
        if self.role == "less" and self.cap_f is not None:
            return None, self.cap_f + 0.5
        raise ValueError(f"cannot map continuity bounds for {self.market_id!r}")


def parse_kalshi_bracket(market_id: str) -> KalshiBracket | None:
    if match := TAIL_ABOVE.search(market_id):
        threshold = int(match.group(1))
        return KalshiBracket(
            market_id=market_id,
            role="greater",
            floor_f=threshold,
            cap_f=None,
        )
    if match := BETWEEN_HALF.search(market_id):
        low = int(match.group(1))
        return KalshiBracket(
            market_id=market_id,
            role="between",
            floor_f=low,
            cap_f=low + 1,
        )
    if match := BETWEEN_INT.search(market_id):
        low = int(match.group(1))
        return KalshiBracket(
            market_id=market_id,
            role="between",
            floor_f=low,
            cap_f=low,
        )
    return None


def brackets_from_market_ids(market_ids: tuple[str, ...] | list[str]) -> tuple[KalshiBracket, ...]:
    parsed: list[KalshiBracket] = []
    for market_id in market_ids:
        row = parse_kalshi_bracket(market_id)
        if row is not None:
            parsed.append(row)
    return tuple(parsed)


def assert_normalised(probs: Mapping[str, Decimal], *, tol: Decimal = Decimal("1e-9")) -> None:
    total = sum(probs.values(), Decimal(0))
    if abs(total - Decimal(1)) > tol:
        raise AssertionError(f"Σ p_hat = {total}, expected 1")
    if any(p < Decimal(0) for p in probs.values()):
        raise AssertionError("negative bracket probability")


def renormalise_clipped(raw: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Clip to (0,1), then renormalise. Asserts Σ=1."""
    clipped = {k: min(Decimal(1), max(Decimal(0), v)) for k, v in raw.items()}
    total = sum(clipped.values(), Decimal(0))
    if total <= Decimal(0):
        raise ValueError("no positive mass after clip")
    out = {k: v / total for k, v in clipped.items()}
    assert_normalised(out)
    return out
