"""Console checklist blocks unverified facts; journal/reconcile flag mismatches."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from wxmm.core.money import Money
from wxmm.core.types import Order
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH
from wxmm.execute.journal import Journal
from wxmm.execute.lifecycle import OrderState
from wxmm.execute.reconcile import VenueFill, reconcile
from wxmm.ops.console import ConsoleState, pretrade_blockers, render
from wxmm.risk.limits import Limits
from wxmm.strategy.view import ProposedOrder

UTC = timezone.utc


def test_console_blocks_expired_maker_fee_and_pm_schedule() -> None:
    state = ConsoleState(
        as_of=datetime(2026, 9, 4, tzinfo=UTC),
        kalshi=(),
        polymarket=(),
        positions=(),
        hedge=None,
        kalshi_tokens_remaining=100.0,
        polymarket_req_remaining=20.0,
    )
    blockers = pretrade_blockers(state)
    assert any("re-verify" in item or "kalshi_weather_maker_fee" in item for item in blockers)
    text = render(state)
    assert "NO ORDER ROUTER" in text
    assert "BLOCKED" in text


def test_journal_intent_sent_filled_and_mismatch() -> None:
    journal = Journal()
    order = Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=2,
        is_taker=True,
        client_intent_id="i1",
    )
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
    journal.propose("i1", order, ts, underlying=KALSHI_NYC_DAILY_HIGH)
    journal.transition("i1", OrderState.APPROVED, at_utc=ts, actor="human", evidence="ok")
    journal.transition("i1", OrderState.SENT, at_utc=ts, actor="human", evidence="sent")
    journal.transition("i1", OrderState.ACKED, at_utc=ts, actor="venue", evidence="ack")
    journal.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=ts, evidence="fill")
    mismatches = reconcile(journal, [VenueFill("v1", "M", 1, 40)]).mismatches
    assert any(item.kind == "qty_mismatch" for item in mismatches)
    mismatches2 = reconcile(journal, [VenueFill("v2", "M", 2, 40)]).mismatches
    assert any(item.kind == "venue_fill_not_in_journal" for item in mismatches2)
    assert any(item.kind == "journal_fill_not_at_venue" for item in mismatches2)


def test_two_venue_fill_ids_reconcile_each_increment() -> None:
    journal = Journal()
    order = Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=2,
        is_taker=True,
        client_intent_id="i1",
    )
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
    journal.propose("i1", order, ts, underlying=KALSHI_NYC_DAILY_HIGH)
    journal.transition("i1", OrderState.APPROVED, at_utc=ts, actor="human", evidence="ok")
    journal.transition("i1", OrderState.SENT, at_utc=ts, actor="human", evidence="sent")
    journal.transition("i1", OrderState.ACKED, at_utc=ts, actor="venue", evidence="ack")
    journal.apply_fill("i1", fill_qty=1, venue_fill_id="v1", at_utc=ts, evidence="p1")
    journal.apply_fill("i1", fill_qty=1, venue_fill_id="v2", at_utc=ts, evidence="p2")
    report = reconcile(
        journal,
        [VenueFill("v1", "M", 1, 40), VenueFill("v2", "M", 1, 40)],
    )
    assert report.mismatches == ()
    assert journal.lifecycle.state_of("i1") is OrderState.FILLED


def test_fill_after_filled_does_not_inflate_position() -> None:
    journal = Journal()
    order = Order(
        venue="kalshi",
        market_id="M",
        side="buy",
        price_cents=40,
        quantity=2,
        is_taker=True,
        client_intent_id="i1",
    )
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
    journal.propose("i1", order, ts, underlying=KALSHI_NYC_DAILY_HIGH)
    journal.transition("i1", OrderState.APPROVED, at_utc=ts, actor="human", evidence="ok")
    journal.transition("i1", OrderState.SENT, at_utc=ts, actor="human", evidence="sent")
    journal.transition("i1", OrderState.ACKED, at_utc=ts, actor="venue", evidence="ack")
    journal.apply_fill("i1", fill_qty=2, venue_fill_id="v1", at_utc=ts, evidence="fill")
    journal.apply_fill("i1", fill_qty=2, venue_fill_id="v-late", at_utc=ts, evidence="late")
    pos = journal.positions().all()
    assert len(pos) == 1
    assert pos[0].quantity == 2
    assert any(rec.kind == "fill_after_terminal_ignored" for rec in journal.records)


def test_console_limit_breach_names_the_limit() -> None:
    state = ConsoleState(
        as_of=datetime(2026, 7, 4, tzinfo=UTC),
        kalshi=(),
        polymarket=(),
        positions=(),
        hedge=None,
        kalshi_tokens_remaining=100.0,
        polymarket_req_remaining=20.0,
        limits=Limits(
            max_notional_per_climate_day=Money.cents(10),
            max_collateral_committed=Money.cents(10),
            max_exposure_per_underlying=Money.cents(10),
            max_residual_basis_pct=Decimal("50"),
        ),
        proposals=(
            ProposedOrder(
                venue="kalshi",
                market="M",
                side="buy",
                price=50,
                size=1,
                rationale="console check",
            ),
        ),
    )
    blockers = pretrade_blockers(state)
    assert any("max_notional_per_climate_day" in item for item in blockers)
