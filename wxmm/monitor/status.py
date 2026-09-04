"""OK | DEGRADED | HALT. HALT is sticky until a journal resolution exists.

Allowed to assume
    HALT on unreconciled break, stale feed, daily-loss, verified fee
    mismatch, or manual. DEGRADED still allows proposals, stamped.

Must never
    Leave HALT without a journal entry that has a reason. Call wall-clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from wxmm.core.errors import FeeMismatch, UnreconciledBreak
from wxmm.core.utc import require_utc
from wxmm.execute.journal import Journal
from wxmm.execute.reconcile import BreakReport
from wxmm.live.health import FeedHealth
from wxmm.monitor.alerts import Alert


class SystemStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    HALT = "HALT"


@dataclass
class StatusMachine:
    journal: Journal
    status: SystemStatus = SystemStatus.OK
    reason: str | None = None
    degradation: tuple[str, ...] = ()
    alerts: list[Alert] = field(default_factory=list)
    daily_loss_limit: object | None = None
    stale_after: timedelta = field(default_factory=lambda: timedelta(seconds=30))

    def halt(self, reason: str, *, at_utc: datetime, actor: str = "human") -> None:
        self.status = SystemStatus.HALT
        self.reason = reason
        self.alerts.append(
            Alert(kind="halt", at_utc=require_utc(at_utc), detail=reason, status=self.status)
        )
        _ = actor

    def halt_from_break(self, report: BreakReport, *, at_utc: datetime) -> None:
        if report.blocking:
            self.halt("unreconciled_break", at_utc=at_utc, actor="human")
            raise UnreconciledBreak("unreconciled break; HALT until journal resolution")

    def halt_from_fee_mismatch(self, exc: FeeMismatch, *, at_utc: datetime) -> None:
        self.halt(f"fee_mismatch:{exc}", at_utc=at_utc, actor="human")
        raise exc

    def note_feeds(self, feeds: tuple[FeedHealth, ...], *, now: datetime) -> None:
        if self.status is SystemStatus.HALT:
            return
        stale = [f.name for f in feeds if f.stale_beyond(now, self.stale_after)]
        down = [f.name for f in feeds if not f.heartbeat_ok]
        if stale:
            self.halt(f"feed_stale:{','.join(stale)}", at_utc=now, actor="human")
            return
        reasons: list[str] = []
        if down:
            reasons.append(f"feed_down:{','.join(down)}")
        if reasons:
            self.status = SystemStatus.DEGRADED
            self.degradation = tuple(reasons)
        elif self.status is SystemStatus.DEGRADED:
            self.status = SystemStatus.OK
            self.degradation = ()

    def note_rate_budget_low(self, *, at_utc: datetime) -> None:
        if self.status is SystemStatus.HALT:
            return
        self.status = SystemStatus.DEGRADED
        self.degradation = tuple(sorted(set(self.degradation + ("rate_budget_low",))))
        _ = at_utc

    def note_venue_unverified(self, venue: str, *, at_utc: datetime) -> None:
        if self.status is SystemStatus.HALT:
            return
        self.status = SystemStatus.DEGRADED
        flag = f"venue_unverified:{venue}"
        self.degradation = tuple(sorted(set(self.degradation + (flag,))))
        _ = at_utc

    def resume(self, reason: str, *, at_utc: datetime) -> None:
        if not reason.strip():
            raise ValueError("leaving HALT requires a reason")
        self.journal.record_resolution(reason, at_utc=at_utc)
        self.status = SystemStatus.OK
        self.reason = None
        self.degradation = ()
        self.alerts.append(
            Alert(
                kind="resume",
                at_utc=require_utc(at_utc),
                detail=reason,
                status=self.status,
            )
        )
