"""Fault injection: journal stays consistent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wxmm.core.errors import RateLimited, StaleBookError
from wxmm.core.money import Money
from wxmm.core.types import FrozenClock, InMemoryAsOfStore, Order
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.core.view import build_market_view
from wxmm.decide.proposal import Proposal
from wxmm.execute.journal import Journal
from wxmm.execute.lifecycle import OrderState
from wxmm.execute.send_gate import SendGate
from wxmm.hedge.executor import HedgeExecutor, HedgeState
from wxmm.live.feed import FakeTransport, Feed
from wxmm.live.health import FeedHealth
from wxmm.live.state import (
    LiveState,
    book_store_key,
    snapshot_payload,
    snapshot_update,
    stale_update,
)
from wxmm.risk.basis import BasisModel
from wxmm.risk.position import Position
from wxmm.venues.fake.adapter import FakeVenue

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
KEY = book_store_key("kalshi", "M")


def _proposal() -> Proposal:
    return Proposal(
        venue="kalshi",
        market="M",
        side="buy",
        price=40,
        size=2,
        modelled_fee=Money.cents(0),
        modelled_collateral=Money.cents(80),
        edge=None,
        limit_checks_passed=("size_known",),
        rate_limit_cost=10.0,
        rationale="chaos",
    )


def _order() -> Order:
    return Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=2,
        is_taker=False,
        client_intent_id="i1",
    )


def test_429_with_and_without_retry_after() -> None:
    clock = FrozenClock(TS)
    venue = FakeVenue(InMemoryAsOfStore())
    gate = SendGate(venue, Journal(), clock)
    p = _proposal()
    gate.journal.propose("i1", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    venue.next_outcome = "429"
    token = gate.issue_token(p)
    result = gate.send(p, token, intent_id="i1")
    assert result.accepted is False
    assert "429" in result.detail
    assert gate.journal.lifecycle.state_of("i1") is OrderState.APPROVED
    gate.journal.propose("i2", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    venue.next_outcome = "429_retry"
    p2 = _proposal()
    token2 = gate.issue_token(p2)
    result2 = gate.send(p2, token2, intent_id="i2")
    assert "retry_after" in result2.detail
    with pytest.raises(RateLimited) as exc:
        venue.next_outcome = "429"
        venue.submit(_order())
    assert exc.value.retry_after is None


def test_disconnect_mid_update_marks_stale() -> None:
    clock = FrozenClock(TS)
    state = LiveState(clock)
    health = FeedHealth("fake")
    u = snapshot_update(
        venue="kalshi",
        market_id="M",
        valid_at=TS,
        available_at=TS,
        payload=snapshot_payload(
            market_id="M", bid_cents=40, ask_cents=42, bid_size=5, ask_size=5
        ),
        source="chaos",
    )
    transport = FakeTransport([u, u], disconnect_after=1)
    feed = Feed(state, health, transport, clock=clock)

    async def _run() -> None:
        await feed.run_once(transport.updates())

    import asyncio

    with pytest.raises(ConnectionError):
        asyncio.run(_run())
    assert health.reconnect_count == 1
    with pytest.raises(StaleBookError):
        state.get(KEY, as_of=TS)


def test_duplicate_fill_does_not_double_position() -> None:
    j = Journal()
    j.propose("i1", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    j.transition("i1", OrderState.APPROVED, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.SENT, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.ACKED, at_utc=TS, actor="venue", evidence="ack")
    j.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=TS, evidence="f")
    j.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=TS, evidence="dup")
    pos = j.positions().all()
    assert len(pos) == 1
    assert pos[0].quantity == 2
    assert any(r.kind == "fill_duplicate_ignored" for r in j.records)


def test_out_of_order_fill_before_ack_is_buffered() -> None:
    j = Journal()
    j.propose("i1", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    j.transition("i1", OrderState.APPROVED, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.SENT, at_utc=TS, actor="human", evidence="e")
    j.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=TS, evidence="early")
    assert j.lifecycle.state_of("i1") is OrderState.SENT
    assert j.positions().all() == ()
    j.transition("i1", OrderState.ACKED, at_utc=TS, actor="venue", evidence="ack")
    pos = j.positions().all()
    assert len(pos) == 1
    assert pos[0].quantity == 2
    assert j.lifecycle.state_of("i1") is OrderState.FILLED


def test_venue_clock_ahead_of_ours() -> None:
    clock = FrozenClock(TS)
    live = LiveState(clock)
    venue_ts = TS + timedelta(minutes=5)
    live.apply(
        snapshot_update(
            venue="kalshi",
            market_id="M",
            valid_at=venue_ts,
            available_at=TS,
            payload=snapshot_payload(
                market_id="M", bid_cents=40, ask_cents=42, bid_size=5, ask_size=5
            ),
            source="ahead",
        )
    )
    view = build_market_view(store=live, clock=clock, book_keys=(("kalshi", KEY),))
    assert view.books[0].bid_cents == 40


def test_stale_book_during_proposal_generation() -> None:
    clock = FrozenClock(TS)
    live = LiveState(clock)
    live.apply(
        snapshot_update(
            venue="kalshi",
            market_id="M",
            valid_at=TS,
            available_at=TS,
            payload=snapshot_payload(
                market_id="M", bid_cents=40, ask_cents=42, bid_size=5, ask_size=5
            ),
            source="t",
        )
    )
    live.apply(
        stale_update(
            venue="kalshi",
            market_id="M",
            valid_at=TS,
            available_at=TS,
            source="reconnect",
        )
    )
    with pytest.raises(StaleBookError):
        build_market_view(store=live, clock=clock, book_keys=(("kalshi", KEY),))


def test_settlement_then_revision() -> None:
    j = Journal()
    j.propose("i1", _order(), TS, underlying=KALSHI_NYC_DAILY_HIGH)
    j.transition("i1", OrderState.APPROVED, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.SENT, at_utc=TS, actor="human", evidence="e")
    j.transition("i1", OrderState.ACKED, at_utc=TS, actor="venue", evidence="ack")
    j.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=TS, evidence="f")
    j.record_settlement("i1", at_utc=TS, high_f=91, revision=False, evidence="first")
    assert j.lifecycle.state_of("i1") is OrderState.SETTLED
    j.record_settlement("i1", at_utc=TS, high_f=90, revision=True, evidence="accept_until_next")
    assert j.lifecycle.state_of("i1") is OrderState.SETTLED
    assert any(r.kind == "settlement_revised_after_settled" for r in j.records)
    kinds = [r.kind for r in j.records]
    assert "settlement" in kinds


def test_partial_hedge_leg2_rejected() -> None:
    pos = Position("kalshi", "A", KALSHI_NYC_DAILY_HIGH, 10, 50)
    model = BasisModel(KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH)
    ex = HedgeExecutor(
        position=pos,
        toward=POLYMARKET_NYC_DAILY_HIGH,
        book_size=Money.cents(100000),
        max_basis_risk_pct=Decimal("100"),
        basis=model,
        season="JJA",
    )
    planned = ex.plan_now(at=TS, hedge_venue="polymarket", hedge_market_id="nyc-76-77")
    assert planned.legs
    ex.note_leg_fill(0, filled_qty=5)
    assert ex.state is HedgeState.PARTIAL
    assert ex.open_exposure != 0
    ex.note_leg_reject(1)
    assert ex.state is HedgeState.PARTIAL
    assert ex.stopped_at_leg == 1
