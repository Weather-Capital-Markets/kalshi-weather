"""In-memory fake venue. Scripted BookUpdates, acks, fills, 429s, disconnects.

Allowed to assume
    Callers inject a clock and a script. Fees default to the Kalshi formula
    when ``fee_like='kalshi'``. Polymarket-like mode raises unverified.
    ``BookUpdate`` objects are the same tape records live and replay consume.

Must never
    Read API keys. Call HTTP. Use wall-clock. Price Polymarket while unverified.
    Import ``wxmm.live``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from wxmm.core.book import BookUpdate, apply_book_update
from wxmm.core.errors import RateLimited, UnverifiedFeeSchedule
from wxmm.core.money import Money
from wxmm.core.types import (
    POLYMARKET_FEE_SCHEDULE,
    AsOfRecord,
    AsOfStore,
    BookSnapshot,
    Order,
    Trade,
)
from wxmm.core.utc import require_utc
from wxmm.settlement.eras import settlement_rule_in_force
from wxmm.settlement.rules import SettlementRule
from wxmm.venues.kalshi.fees import order_fee

SubmitOutcome = Literal["ack", "reject", "429", "429_retry", "disconnect"]


@dataclass
class FakeAck:
    order: Order
    venue_order_id: str
    accepted: bool
    rejected: bool
    reason: str = ""


@dataclass
class FakeFill:
    venue_fill_id: str
    venue_order_id: str
    market_id: str
    quantity: int
    price_cents: int
    fee: Money | None
    ts: datetime


class FakeVenue:
    """Scripted venue used by send-gate and chaos tests."""

    def __init__(
        self,
        store: AsOfStore,
        *,
        fee_like: str = "kalshi",
        name: str = "fake",
        tape: Sequence[BookUpdate] = (),
    ) -> None:
        self.name = name
        self.fee_like = fee_like
        self._store = store
        self.submitted: list[Order] = []
        self.acks: list[FakeAck] = []
        self.fills: list[FakeFill] = []
        self.next_outcome: SubmitOutcome = "ack"
        self.retry_after: float | None = 1.0
        self.disconnected = False
        self.venue_clock_ahead: timedelta = timedelta(0)
        self._seq = 0
        self._tape: list[BookUpdate] = list(tape)
        self.emitted: list[BookUpdate] = []

    def list_markets(self, as_of: datetime) -> tuple[str, ...]:
        rec = _optional_get(self._store, "fake:markets", as_of)
        if rec is None:
            return ()
        payload = rec.payload
        if isinstance(payload, (list, tuple)):
            return tuple(str(item) for item in payload)
        return ()

    def book_at(self, market: str, as_of: datetime) -> BookSnapshot:
        rec = self._store.get(f"fake:book:{market}", as_of)
        if isinstance(rec.payload, BookSnapshot):
            return rec.payload
        raise TypeError(f"fake book payload for {market!r} is not a BookSnapshot")

    def trades(
        self,
        market: str,
        window: tuple[datetime, datetime],
        as_of: datetime,
    ) -> tuple[Trade, ...]:
        start, end = require_utc(window[0]), require_utc(window[1])
        rec = _optional_get(self._store, f"fake:trades:{market}", as_of)
        if rec is None:
            return ()
        payload = rec.payload
        if not isinstance(payload, (list, tuple)):
            return ()
        out: list[Trade] = []
        for item in payload:
            if not isinstance(item, Trade):
                continue
            if start <= item.ts < end and item.available_at <= require_utc(as_of):
                out.append(item)
        return tuple(out)

    def fee(self, order: Order) -> Money:
        if self.fee_like == "polymarket":
            raise UnverifiedFeeSchedule(
                f"fake venue in polymarket mode: {POLYMARKET_FEE_SCHEDULE.name} "
                f"is {POLYMARKET_FEE_SCHEDULE.status.value}"
            )
        return order_fee(order)

    def rate_limits(self) -> dict[str, float]:
        if self.fee_like == "polymarket":
            return {"requests_per_sec_per_ip": 20.0}
        return {
            "write_tokens_per_sec": 100.0,
            "orders_per_sec_approx": 10.0,
            "batch_cancel_tokens": 2.0,
        }

    def settlement_rule(self, as_of: datetime) -> SettlementRule:
        like = "polymarket" if self.fee_like == "polymarket" else "kalshi"
        return settlement_rule_in_force(like, as_of)

    def queue_book_update(self, update: BookUpdate) -> None:
        self._tape.append(update)

    def scripted_book_updates(self) -> tuple[BookUpdate, ...]:
        return tuple(self._tape)

    def emit_book_updates(self) -> tuple[BookUpdate, ...]:
        """Write the scripted tape onto the venue store. Same records replay uses."""
        out = tuple(self._tape)
        for update in out:
            apply_book_update(self._store, update)
            self.emitted.append(update)
        return out

    def submit(self, order: Order) -> FakeAck:
        """Only ``wxmm.execute.send_gate`` should call this from package code."""
        if self.disconnected:
            raise ConnectionError("fake venue disconnected")
        outcome = self.next_outcome
        if outcome == "429":
            raise RateLimited("fake 429", retry_after=None)
        if outcome == "429_retry":
            raise RateLimited("fake 429", retry_after=self.retry_after)
        if outcome == "disconnect":
            self.disconnected = True
            raise ConnectionError("fake venue disconnected mid-submit")
        self.submitted.append(order)
        self._seq += 1
        ack = FakeAck(
            order=order,
            venue_order_id=f"fake-{self._seq}",
            accepted=outcome == "ack",
            rejected=outcome == "reject",
            reason="" if outcome == "ack" else "fake_reject",
        )
        self.acks.append(ack)
        return ack

    def inject_fill(
        self,
        *,
        venue_order_id: str,
        market_id: str,
        quantity: int,
        price_cents: int,
        ts: datetime,
        fee: Money | None = None,
        venue_fill_id: str | None = None,
    ) -> FakeFill:
        self._seq += 1
        fill = FakeFill(
            venue_fill_id=venue_fill_id or f"ffill-{self._seq}",
            venue_order_id=venue_order_id,
            market_id=market_id,
            quantity=quantity,
            price_cents=price_cents,
            fee=fee,
            ts=require_utc(ts),
        )
        self.fills.append(fill)
        return fill


def _optional_get(store: AsOfStore, key: str, as_of: datetime) -> AsOfRecord | None:
    from wxmm.core.errors import MissingDataError

    try:
        return store.get(key, as_of)
    except MissingDataError:
        return None
