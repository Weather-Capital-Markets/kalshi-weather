"""Live market state. Same BookSource as replay.

Allowed to assume
    Updates arrive as immutable ``BookUpdate`` records with ``valid_at``
    (venue timestamp) and ``available_at`` (receipt / poll-completion).
    The bound clock is the only 'now' for reads.

Must never
    Interpret updates (no trading meaning). Return a STALE book. Import
    ``wxmm.backtest``. Call ``datetime.now``. Read credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from wxmm.core.types import (
    AsOfRecord,
    AsOfStore,
    BookSnapshot,
    Clock,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
    clip_extreme_touch,
    received_record,
)
from wxmm.core.underlying import Underlying
from wxmm.core.utc import require_utc


def book_store_key(venue: str, market_id: str) -> str:
    return f"{venue}:book:{market_id}"


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """Transport output. Never interpreted by the transport that emits it."""

    key: str
    venue: str
    market_id: str
    valid_at: datetime
    available_at: datetime
    payload: object | None
    stale: bool
    source: str
    ingest_run_id: str = "live"
    seq: int | None = None
    underlying: Underlying | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_at", require_utc(self.valid_at))
        object.__setattr__(self, "available_at", require_utc(self.available_at))
        if self.stale and self.payload is not None:
            raise ValueError("STALE BookUpdate must not carry a payload")
        if not self.stale and self.payload is None:
            raise ValueError("non-STALE BookUpdate requires a payload")

    def to_record(self) -> AsOfRecord:
        return received_record(
            key=self.key,
            payload=self.payload,
            valid_at=self.valid_at,
            received_at=self.available_at,
            source=self.source,
            ingest_run_id=self.ingest_run_id,
            availability="stale" if self.stale else "known",
        )


def apply_book_update(store: AsOfStore, update: BookUpdate) -> None:
    """Put the update onto any as-of store. Shared by live and replay."""
    store.put(update.to_record())


class LiveState:
    """Clock-bound book source. STALE keys raise ``StaleBookError`` on ``get``."""

    is_clock_bound_book_source: bool = True

    def __init__(
        self,
        clock: Clock,
        inner: AsOfStore | None = None,
    ) -> None:
        self._clock = clock
        self._inner: AsOfStore = inner if inner is not None else InMemoryAsOfStore()
        self._bound = ClockBoundStore(self._inner, clock)
        self._meta: dict[str, tuple[str, str]] = {}
        self._underlying: dict[str, Underlying] = {}

    @property
    def clock(self) -> Clock:
        return self._clock

    def apply(self, update: BookUpdate) -> None:
        apply_book_update(self._bound, update)
        self._meta[update.key] = (update.venue, update.market_id)
        if update.underlying is not None:
            self._underlying[update.key] = update.underlying

    def mark_all_stale(self, *, source: str = "reconnect") -> None:
        now = self._clock.now()
        for key, (venue, market_id) in list(self._meta.items()):
            self.apply(
                BookUpdate(
                    key=key,
                    venue=venue,
                    market_id=market_id,
                    valid_at=now,
                    available_at=now,
                    payload=None,
                    stale=True,
                    source=source,
                    ingest_run_id="reconnect",
                    underlying=self._underlying.get(key),
                )
            )

    def get(self, key: str, as_of: datetime) -> AsOfRecord:
        return self._bound.get(key, as_of)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._meta.keys())

    def book_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple((venue, key) for key, (venue, _) in self._meta.items())

    def underlying_for(self, key: str) -> Underlying | None:
        return self._underlying.get(key)


def snapshot_payload(
    *,
    market_id: str,
    bid_cents: int | None,
    ask_cents: int | None,
    bid_size: int | None,
    ask_size: int | None,
    volume: int | None = None,
    two_sided: bool | None = None,
) -> dict[str, object]:
    bid_cents, ask_cents = clip_extreme_touch(bid_cents, ask_cents)
    if bid_cents is None:
        bid_size = None
    if ask_cents is None:
        ask_size = None
    sided = two_sided
    if sided is None:
        sided = bid_cents is not None and ask_cents is not None
    else:
        sided = bool(sided) and bid_cents is not None and ask_cents is not None
    return {
        "market_id": market_id,
        "yes_bid_cents": bid_cents,
        "yes_ask_cents": ask_cents,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "volume": volume,
        "two_sided": bool(sided),
        "reconstructed": False,
    }


def snapshot_update(
    *,
    venue: str,
    market_id: str,
    valid_at: datetime,
    available_at: datetime,
    payload: Mapping[str, object] | BookSnapshot,
    source: str,
    key: str | None = None,
    ingest_run_id: str = "live",
    seq: int | None = None,
    underlying: Underlying | None = None,
) -> BookUpdate:
    return BookUpdate(
        key=key or book_store_key(venue, market_id),
        venue=venue,
        market_id=market_id,
        valid_at=valid_at,
        available_at=available_at,
        payload=dict(payload) if isinstance(payload, Mapping) else payload,
        stale=False,
        source=source,
        ingest_run_id=ingest_run_id,
        seq=seq,
        underlying=underlying,
    )


def stale_update(
    *,
    venue: str,
    market_id: str,
    valid_at: datetime,
    available_at: datetime,
    source: str,
    key: str | None = None,
    ingest_run_id: str = "reconnect",
    underlying: Underlying | None = None,
) -> BookUpdate:
    return BookUpdate(
        key=key or book_store_key(venue, market_id),
        venue=venue,
        market_id=market_id,
        valid_at=valid_at,
        available_at=available_at,
        payload=None,
        stale=True,
        source=source,
        ingest_run_id=ingest_run_id,
        underlying=underlying,
    )


def live_state_with_frozen_clock(ts: datetime, inner: AsOfStore | None = None) -> LiveState:
    return LiveState(FrozenClock(ts), inner)
