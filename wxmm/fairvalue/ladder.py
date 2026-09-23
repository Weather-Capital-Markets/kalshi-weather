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
from typing import Mapping, Sequence

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


def _threshold(bracket: KalshiBracket) -> int:
    if bracket.floor_f is not None:
        return bracket.floor_f
    if bracket.cap_f is not None:
        return bracket.cap_f
    raise ValueError(f"ladder_order cannot orient {bracket.market_id!r}")


def _orient_ladder(tickers: Sequence[str]) -> list[str]:
    """Temperature order. A tail at or under the lowest between-floor is below;
    a tail at or over the highest between-cap is above. Two tails and no
    between-brackets: the lower threshold is below. Anything else raises.
    """
    if not tickers:
        raise ValueError("ladder_order cannot orient an empty ladder")
    if len(set(tickers)) != len(tickers):
        raise ValueError(f"ladder_order cannot orient duplicates in {list(tickers)!r}")
    parsed: list[tuple[str, KalshiBracket]] = []
    for ticker in tickers:
        row = parse_kalshi_bracket(ticker)
        if row is None:
            raise ValueError(f"ladder_order cannot orient {ticker!r}")
        parsed.append((ticker, row))
    betweens = [(ticker, row) for ticker, row in parsed if row.role == "between"]
    tails = [(ticker, row) for ticker, row in parsed if row.role != "between"]
    if betweens:
        floors = [row.floor_f for _, row in betweens]
        caps = [row.cap_f for _, row in betweens]
        if any(floor is None for floor in floors) or any(cap is None for cap in caps):
            raise ValueError(f"ladder_order cannot orient {list(tickers)!r}")
        lowest_floor = min(floor for floor in floors if floor is not None)
        highest_cap = max(cap for cap in caps if cap is not None)
        below: list[tuple[int, str]] = []
        above: list[tuple[int, str]] = []
        for ticker, row in tails:
            threshold = _threshold(row)
            is_below = threshold <= lowest_floor
            is_above = threshold >= highest_cap
            if is_below and is_above:
                raise ValueError(f"ladder_order cannot orient {ticker!r}")
            if is_below:
                below.append((threshold, ticker))
            elif is_above:
                above.append((threshold, ticker))
            else:
                raise ValueError(f"ladder_order cannot orient {ticker!r}")
        if len(below) > 1 or len(above) > 1:
            raise ValueError(f"ladder_order cannot orient {list(tickers)!r}")
        between_sorted = sorted(
            betweens,
            key=lambda item: (
                item[1].floor_f if item[1].floor_f is not None else 0,
                item[1].cap_f if item[1].cap_f is not None else 0,
                item[0],
            ),
        )
        ordered = [ticker for _threshold_f, ticker in sorted(below)]
        ordered.extend(ticker for ticker, _row in between_sorted)
        ordered.extend(ticker for _threshold_f, ticker in sorted(above))
        return ordered
    if len(tails) != 2:
        raise ValueError(f"ladder_order cannot orient {list(tickers)!r}")
    first, second = tails
    left, right = _threshold(first[1]), _threshold(second[1])
    if left == right:
        raise ValueError(f"ladder_order cannot orient {list(tickers)!r}")
    if left < right:
        return [first[0], second[0]]
    return [second[0], first[0]]


def ladder_order(tickers: Sequence[str]) -> list[str]:
    """Temperature order of a KXHIGHNY ladder. Refuses a ladder it cannot orient."""
    return _orient_ladder(tickers)


def oriented_continuity_bounds(
    ticker: str, ladder: Sequence[str]
) -> tuple[float | None, float | None]:
    """Half-degree bounds after the tail is placed by ``ladder_order``.

    Every ``T`` suffix parses as greater, so ``continuity_bounds_f`` on a
    bottom tail is ``(threshold - 0.5, None)``. A tail that temperature order
    places first, and not also last, is the bottom tail ``(None, threshold - 0.5)``.
    A tail placed last is the top. Between-brackets keep ``continuity_bounds_f``.
    """
    bracket = parse_kalshi_bracket(ticker)
    if bracket is None:
        raise ValueError(f"cannot map continuity bounds for {ticker!r}")
    if bracket.role == "between":
        return bracket.continuity_bounds_f()
    ordered = ladder_order(ladder)
    if ticker not in ordered or ordered[0] == ordered[-1]:
        raise ValueError(f"cannot map continuity bounds for {ticker!r}")
    threshold = float(_threshold(bracket)) - 0.5
    if ordered[0] == ticker:
        return None, threshold
    if ordered[-1] == ticker:
        return threshold, None
    raise ValueError(f"cannot map continuity bounds for {ticker!r}")


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
