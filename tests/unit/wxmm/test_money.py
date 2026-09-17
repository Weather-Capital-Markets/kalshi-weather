"""Money is Decimal, never float."""

from __future__ import annotations

from decimal import Decimal

from wxmm.core.money import Money


def test_cents_constructor() -> None:
    assert Money.cents(7) == Money.dollars(Decimal("0.07"))


def test_round_up_to_cent() -> None:
    fee = Money.dollars(Decimal("0.0175"))
    assert fee.round_up_to_cent() == Money.cents(2)


def test_zero_is_not_float() -> None:
    z = Money.zero()
    assert isinstance(z.amount, Decimal)
    assert z == Money.cents(0)


def test_addition() -> None:
    assert Money.cents(10) + Money.cents(3) == Money.cents(13)
