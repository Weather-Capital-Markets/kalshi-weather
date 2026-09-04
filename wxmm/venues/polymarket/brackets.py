"""Polymarket NYC daily-high bracket enumerator.

The 2°F exhaustive ladder is a given domain fact (prompt + venue-facts §3.1).
Kalshi bracket widths must NOT be copied from this table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bracket:
    label: str
    low_f: int | None
    high_f: int | None
    kind: str

    def contains(self, high_f: int) -> bool:
        if self.low_f is None and self.high_f is not None:
            return high_f <= self.high_f
        if self.high_f is None and self.low_f is not None:
            return high_f >= self.low_f
        if self.low_f is not None and self.high_f is not None:
            return self.low_f <= high_f <= self.high_f
        return False


def polymarket_nyc_brackets() -> tuple[Bracket, ...]:
    """≤75, 76-77 … 92-93, ≥94. Eleven disjoint contracts."""
    interior: list[Bracket] = []
    low = 76
    while low <= 92:
        interior.append(
            Bracket(
                label=f"{low}-{low + 1}",
                low_f=low,
                high_f=low + 1,
                kind="disjoint_bin",
            )
        )
        low += 2
    return (
        Bracket(label="<=75", low_f=None, high_f=75, kind="tail_below"),
        *interior,
        Bracket(label=">=94", low_f=94, high_f=None, kind="tail_above"),
    )


def assert_eleven_exhaustive() -> None:
    brackets = polymarket_nyc_brackets()
    if len(brackets) != 11:
        raise AssertionError(f"expected 11 Polymarket NYC brackets, got {len(brackets)}")
