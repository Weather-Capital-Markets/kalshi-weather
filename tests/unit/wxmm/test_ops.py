"""Console checklist blocks unverified facts; journal/reconcile flag mismatches."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from wxmm.core.money import Money
from wxmm.core.types import Order
from wxmm.ops.console import ConsoleState, pretrade_blockers, render
from wxmm.ops.journal import Journal
from wxmm.ops.reconcile import VenueFill, reconcile
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
    journal.propose("i1", order, ts)
    journal.mark_sent("i1", ts)
    journal.mark_filled("i1", ts, fill_qty=2, venue_fill_id="v1")
    mismatches = reconcile(journal, [VenueFill("v1", "M", 1, 40)])
    assert any(item.kind == "qty_mismatch" for item in mismatches)
    mismatches2 = reconcile(journal, [VenueFill("v2", "M", 2, 40)])
    assert any(item.kind == "venue_fill_not_in_journal" for item in mismatches2)
    assert any(item.kind == "journal_fill_not_at_venue" for item in mismatches2)


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
