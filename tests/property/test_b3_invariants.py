"""Journal replay, lifecycle graph, residual, HALT emit-nothing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wxmm.core.errors import InvalidTransition
from wxmm.core.money import Money
from wxmm.core.types import Order
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.decide.engine import propose
from wxmm.execute.journal import Journal, replay_journal
from wxmm.execute.lifecycle import GRAPH, OrderState, require_transition
from wxmm.hedge.residual import residual_for
from wxmm.risk.basis import BasisModel
from wxmm.risk.position import Position
from wxmm.strategy.view import BookView, MarketView

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def _order() -> Order:
    return Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=2,
        is_taker=False,
    )


def test_journal_replay_idempotent_positions() -> None:
    j = Journal()
    j.propose("i1", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    j.transition("i1", OrderState.APPROVED, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.SENT, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.ACKED, at_utc=TS, actor="venue", evidence="ack")
    j.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=TS, evidence="f", price_cents=40)
    snap = j.positions_fingerprint()
    assert snap
    rebuilt = replay_journal(j.records, seed=j)
    assert rebuilt.positions_fingerprint() == snap
    rebuilt2 = replay_journal(rebuilt.records, seed=j)
    assert rebuilt2.positions_fingerprint() == snap
    from_empty = replay_journal(j.records)
    assert from_empty.positions_fingerprint() == snap


def test_lifecycle_graph_exhaustive_pairs() -> None:
    states: list[OrderState | None] = [None, *list(OrderState)]
    for frm, to in product(states, list(OrderState)):
        allowed = to in GRAPH.get(frm, frozenset())
        if allowed:
            require_transition(frm, to)
        else:
            with pytest.raises(InvalidTransition):
                require_transition(frm, to)


def test_residual_never_zero_across_underlyings_and_monotone_in_size() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    masses: list[Decimal] = []
    for qty in (1, 5, 10):
        res = residual_for(
            pos,
            POLYMARKET_NYC_DAILY_HIGH,
            unhedged_quantity=qty,
            book_size=Money.cents(100000),
            max_basis_risk_pct=Decimal("100"),
            basis=model,
            season="JJA",
        )
        assert res.residual_basis_risk_pct > Decimal("0")
        assert res.same_underlying is False
        masses.append(res.residual_basis_risk_pct * Decimal(abs(qty)))
    assert masses[0] <= masses[1] <= masses[2]


def test_residual_zero_size_still_nonzero_pct() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    res = residual_for(
        pos,
        POLYMARKET_NYC_DAILY_HIGH,
        unhedged_quantity=0,
        book_size=Money.cents(100000),
        max_basis_risk_pct=Decimal("100"),
        basis=model,
        season="overall",
        knyc_max_band="middle_39_85",
    )
    assert res.residual_basis_risk_pct > Decimal("0")


def test_residual_zero_unhedged_does_not_raise_when_pct_exceeds_cap() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    res = residual_for(
        pos,
        POLYMARKET_NYC_DAILY_HIGH,
        unhedged_quantity=0,
        book_size=Money.cents(100000),
        max_basis_risk_pct=Decimal("10"),
        basis=model,
        season="JJA",
    )
    assert res.unhedged_quantity == 0
    assert res.residual_basis_risk_pct == Decimal("61.5")
    assert res.same_underlying is False


def test_halt_emits_no_proposal() -> None:
    view = MarketView(
        books=(
            BookView(
                venue="kalshi",
                market_id="M",
                two_sided=True,
                bid_cents=40,
                ask_cents=42,
                bid_size=5,
                ask_size=5,
                ask_size_known=True,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
        ),
        positions=(),
        fills=(),
    )
    assert propose(view, system_status="HALT") == []


@given(st.sampled_from(["OK", "DEGRADED", "HALT"]), st.integers(1, 3))
@settings(max_examples=20)
def test_halt_no_input_emits_proposal(status: str, n_books: int) -> None:
    books = tuple(
        BookView(
            venue="kalshi",
            market_id=f"M{i}",
            two_sided=True,
            bid_cents=40,
            ask_cents=42,
            bid_size=5,
            ask_size=5,
            ask_size_known=True,
            volume=1,
            reconstructed=False,
            staleness=timedelta(0),
        )
        for i in range(n_books)
    )
    view = MarketView(books=books, positions=(), fills=())
    if status == "HALT":
        assert propose(view, system_status=status) == []
    else:
        out = propose(view, system_status=status)
        if status == "DEGRADED":
            assert all(p.degradation for p in out)
