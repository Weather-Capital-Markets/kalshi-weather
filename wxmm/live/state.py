"""Live market state. Same BookSource as replay.

Allowed to assume
    Updates arrive as immutable ``BookUpdate`` records with ``valid_at``
    (venue timestamp) and ``available_at`` (receipt / poll-completion).
    The bound clock is the only 'now' for reads. Keys are Underlying +
    venue market id when the underlying is known.

Must never
    Interpret updates (no trading meaning). Return a STALE book. Import
    ``wxmm.backtest``. Call ``datetime.now``. Read credentials.
    Construct ``MarketView`` — that lives in ``wxmm.core.view``.
"""

from __future__ import annotations

from datetime import datetime

from wxmm.core.book import (
    BookUpdate,
    apply_book_update,
    book_store_key,
    snapshot_payload,
    snapshot_update,
    stale_update,
)
from wxmm.core.types import (
    AsOfRecord,
    AsOfStore,
    Clock,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
)
from wxmm.core.underlying import Underlying

__all__ = [
    "BookUpdate",
    "LiveState",
    "apply_book_update",
    "book_store_key",
    "live_state_with_frozen_clock",
    "snapshot_payload",
    "snapshot_update",
    "stale_update",
]


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
        self._by_identity: dict[tuple[Underlying, str, str], str] = {}

    @property
    def clock(self) -> Clock:
        return self._clock

    def apply(self, update: BookUpdate) -> None:
        apply_book_update(self._bound, update)
        self._meta[update.key] = (update.venue, update.market_id)
        if update.underlying is not None:
            self._underlying[update.key] = update.underlying
            self._by_identity[(update.underlying, update.venue, update.market_id)] = (
                update.key
            )

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

    def get_identity(
        self,
        underlying: Underlying,
        venue: str,
        market_id: str,
        as_of: datetime,
    ) -> AsOfRecord:
        key = self._by_identity.get((underlying, venue, market_id))
        if key is None:
            key = book_store_key(venue, market_id, underlying)
        return self.get(key, as_of)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._meta.keys())

    def book_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple((venue, key) for key, (venue, _) in self._meta.items())

    def underlying_for(self, key: str) -> Underlying | None:
        return self._underlying.get(key)


def live_state_with_frozen_clock(ts: datetime, inner: AsOfStore | None = None) -> LiveState:
    return LiveState(FrozenClock(ts), inner)
