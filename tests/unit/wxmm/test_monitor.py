"""HALT blocks proposals; resume requires a journal reason."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.core.errors import UnreconciledBreak
from wxmm.core.money import Money
from wxmm.core.types import FrozenClock
from wxmm.decide.engine import propose
from wxmm.execute.journal import Journal
from wxmm.execute.reconcile import BreakReport, Mismatch
from wxmm.live.health import FeedHealth
from wxmm.monitor.status import StatusMachine, SystemStatus
from wxmm.strategy.view import BookView, MarketView

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def test_halt_from_break_and_resume() -> None:
    journal = Journal()
    machine = StatusMachine(journal)
    report = BreakReport(mismatches=(Mismatch("qty_mismatch", "i1", "x"),), halted=True)
    with pytest.raises(UnreconciledBreak):
        machine.halt_from_break(report, at_utc=TS)
    assert machine.status is SystemStatus.HALT
    view = MarketView(
        books=(
            BookView(
                venue="kalshi",
                market_id="M",
                two_sided=True,
                bid_cents=40,
                ask_cents=42,
                bid_size=1,
                ask_size=1,
                ask_size_known=True,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
        ),
        positions=(),
        fills=(),
    )
    assert propose(view, system_status=machine.status.value) == []
    with pytest.raises(ValueError):
        machine.resume("   ", at_utc=TS)
    machine.resume("human reviewed break", at_utc=TS)
    assert machine.status is SystemStatus.OK
    assert journal.latest_resolution_reason() == "human reviewed break"


def test_stale_feed_halts() -> None:
    journal = Journal()
    machine = StatusMachine(journal, stale_after=timedelta(seconds=5))
    health = FeedHealth("kalshi")
    clock = FrozenClock(TS)
    health.note_update(clock.now())
    clock.set(TS + timedelta(seconds=6))
    machine.note_feeds((health,), now=clock.now())
    assert machine.status is SystemStatus.HALT
    assert machine.reason is not None and machine.reason.startswith("feed_stale:")


def test_unseen_feed_is_degraded_not_halt() -> None:
    journal = Journal()
    machine = StatusMachine(journal, stale_after=timedelta(seconds=5))
    health = FeedHealth("kalshi")
    machine.note_feeds((health,), now=TS)
    assert machine.status is SystemStatus.DEGRADED
    assert machine.degradation == ("feed_unseen:kalshi",)


def test_resume_from_ok_raises_and_does_not_journal() -> None:
    journal = Journal()
    machine = StatusMachine(journal)
    with pytest.raises(ValueError, match="HALT"):
        machine.resume("oops", at_utc=TS)
    assert machine.status is SystemStatus.OK
    assert journal.latest_resolution_reason() is None


def test_daily_loss_halts() -> None:
    journal = Journal()
    machine = StatusMachine(journal, daily_loss_limit=Money.cents(50))
    machine.note_daily_loss(Money.cents(40), at_utc=TS)
    assert machine.status is SystemStatus.OK
    machine.note_daily_loss(Money.cents(51), at_utc=TS)
    assert machine.status is SystemStatus.HALT
    assert machine.reason == "daily_loss"
