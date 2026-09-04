"""Cash as ``Decimal`` dollars. Never float.

Allowed to assume
    Venue prices are linear cents on [0, 1] for Kalshi YES contracts
    (``knowledge/venue-facts.md`` §1.5). Maker/taker fee *schedules* live in
    venue adapters, not here.

Must never
    Use binary floating point for cash or fees. Round Polymarket fees (the
    cost model must refuse while unverified). Infer a currency scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Union

DecimalLike = Union[Decimal, int, str]


def _as_decimal(value: DecimalLike) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(value)


@dataclass(frozen=True, slots=True)
class Money:
    """Signed cash amount in dollars, stored as ``Decimal``.

    Integer cents are the preferred constructor for settled cash
    (``Money.cents(7)`` is $0.07). Sub-cent amounts are allowed only for the
    continuous Kalshi fee rounding policy.
    """

    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _as_decimal(self.amount))

    @classmethod
    def zero(cls) -> Money:
        return cls(Decimal("0"))

    @classmethod
    def cents(cls, cents: int) -> Money:
        return cls(Decimal(cents) / Decimal(100))

    @classmethod
    def dollars(cls, dollars: DecimalLike) -> Money:
        return cls(_as_decimal(dollars))

    def to_cents_floor(self) -> int:
        """Whole cents toward −∞. Not a fee rounder."""
        return int((self.amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_EVEN))

    def round_up_to_cent(self) -> Money:
        """Kalshi ``per_order_cent`` policy: ceiling to the next cent."""
        cents = (self.amount * Decimal(100)).to_integral_value(rounding=ROUND_CEILING)
        return Money(cents / Decimal(100))

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.amount - other.amount)

    def __neg__(self) -> Money:
        return Money(-self.amount)

    def __mul__(self, scalar: DecimalLike) -> Money:
        return Money(self.amount * _as_decimal(scalar))

    def __rmul__(self, scalar: DecimalLike) -> Money:
        return self.__mul__(scalar)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self.amount == other.amount
        return NotImplemented

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount <= other.amount

    def __hash__(self) -> int:
        return hash(self.amount)

    def __repr__(self) -> str:
        return f"Money({str(self.amount)!r})"
