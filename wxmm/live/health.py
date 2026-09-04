"""Per-feed heartbeat, last-update age, reconnect and gap counts.

Allowed to assume
    The bound clock is the only 'now'. Monitor reads these counters.

Must never
    Call ``datetime.now``. Mark a book readable. Import ``wxmm.backtest``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wxmm.core.utc import require_utc


@dataclass
class FeedHealth:
    name: str
    last_update_at: datetime | None = None
    reconnect_count: int = 0
    gap_count: int = 0
    last_seq: int | None = None
    heartbeat_ok: bool = True
    _events: list[str] = field(default_factory=list)

    def last_update_age(self, now: datetime) -> timedelta | None:
        if self.last_update_at is None:
            return None
        return require_utc(now) - self.last_update_at

    def note_update(self, now: datetime, *, seq: int | None = None) -> None:
        self.last_update_at = require_utc(now)
        self.heartbeat_ok = True
        if seq is not None and self.last_seq is not None and seq > self.last_seq + 1:
            self.gap_count += seq - self.last_seq - 1
            self._events.append(f"gap:{self.last_seq}->{seq}")
        if seq is not None:
            self.last_seq = seq

    def note_reconnect(self, now: datetime) -> None:
        self.reconnect_count += 1
        self.heartbeat_ok = False
        self._events.append(f"reconnect:{require_utc(now).isoformat()}")

    def stale_beyond(self, now: datetime, threshold: timedelta) -> bool:
        age = self.last_update_age(now)
        if age is None:
            return True
        return age > threshold
