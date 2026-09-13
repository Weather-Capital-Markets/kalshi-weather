"""Null recovery: beta=0 reproduces normalised market ladder."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from wxmm.fairvalue.anchor import (
    market_implied,
    normalised_market_ladder,
    null_recovery_probs,
)
from wxmm.strategy.view import BookView, MarketView


def _bracket_view() -> MarketView:
    books = (
        BookView(
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-B84.5",
            two_sided=True,
            bid_cents=20,
            ask_cents=24,
            bid_size=None,
            ask_size=None,
            ask_size_known=False,
            volume=10,
            reconstructed=False,
            staleness=timedelta(0),
        ),
        BookView(
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-B85.5",
            two_sided=True,
            bid_cents=30,
            ask_cents=34,
            bid_size=None,
            ask_size=None,
            ask_size_known=False,
            volume=10,
            reconstructed=False,
            staleness=timedelta(0),
        ),
        BookView(
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-T86",
            two_sided=True,
            bid_cents=40,
            ask_cents=44,
            bid_size=None,
            ask_size=None,
            ask_size_known=False,
            volume=10,
            reconstructed=False,
            staleness=timedelta(0),
        ),
    )
    return MarketView(books=books, positions=(), fills=())


def test_null_recovery_matches_normalised_mids() -> None:
    view = _bracket_view()
    ladder = market_implied(view)
    expected = normalised_market_ladder(ladder)
    recovered = null_recovery_probs(ladder)
    for key in expected:
        assert abs(recovered[key] - expected[key]) < Decimal("1e-9")
    total = sum(recovered.values(), Decimal(0))
    assert abs(total - Decimal(1)) < Decimal("1e-9")


def test_one_sided_bracket_stays_none() -> None:
    view = MarketView(
        books=(
            BookView(
                venue="kalshi",
                market_id="M1",
                two_sided=False,
                bid_cents=40,
                ask_cents=None,
                bid_size=1,
                ask_size=None,
                ask_size_known=False,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
        ),
        positions=(),
        fills=(),
    )
    ladder = market_implied(view)
    assert ladder.brackets[0].implied_prob is None
