"""Intent → sent → filled, by hand. No router."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from wxmm.core.types import Order
from wxmm.core.utc import require_utc


class JournalState(str, Enum):
    INTENT = "intent"
    SENT = "sent"
    FILLED = "filled"


@dataclass
class JournalEntry:
    intent_id: str
    order: Order
    state: JournalState
    ts: datetime
    fill_qty: int | None = None
    venue_fill_id: str | None = None
    note: str = ""


class Journal:
    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []

    def propose(self, intent_id: str, order: Order, ts: datetime) -> JournalEntry:
        entry = JournalEntry(
            intent_id=intent_id,
            order=order,
            state=JournalState.INTENT,
            ts=require_utc(ts),
        )
        self.entries.append(entry)
        return entry

    def mark_sent(self, intent_id: str, ts: datetime, note: str = "human sent") -> JournalEntry:
        return self._advance(intent_id, JournalState.SENT, ts, note=note)

    def mark_filled(
        self,
        intent_id: str,
        ts: datetime,
        *,
        fill_qty: int,
        venue_fill_id: str,
    ) -> JournalEntry:
        return self._advance(
            intent_id,
            JournalState.FILLED,
            ts,
            fill_qty=fill_qty,
            venue_fill_id=venue_fill_id,
        )

    def _advance(
        self,
        intent_id: str,
        state: JournalState,
        ts: datetime,
        *,
        note: str = "",
        fill_qty: int | None = None,
        venue_fill_id: str | None = None,
    ) -> JournalEntry:
        current = self.latest(intent_id)
        entry = JournalEntry(
            intent_id=intent_id,
            order=current.order,
            state=state,
            ts=require_utc(ts),
            fill_qty=fill_qty if fill_qty is not None else current.fill_qty,
            venue_fill_id=venue_fill_id or current.venue_fill_id,
            note=note,
        )
        self.entries.append(entry)
        return entry

    def latest(self, intent_id: str) -> JournalEntry:
        for entry in reversed(self.entries):
            if entry.intent_id == intent_id:
                return entry
        raise KeyError(f"no journal intent {intent_id}")

    def by_state(self, state: JournalState) -> tuple[JournalEntry, ...]:
        latest_ids = {entry.intent_id: entry for entry in self.entries}
        return tuple(item for item in latest_ids.values() if item.state is state)
