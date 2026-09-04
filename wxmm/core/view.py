"""Single ``MarketView`` constructor. Live and replay both call this.

Allowed to assume
    The store is a clock-bound ``BookSource`` (``ClockBoundStore`` or
    ``LiveState``). Records have already been filtered by ``available_at``.

Must never
    Build a live-only view. Import ``wxmm.live`` or ``wxmm.backtest``.
    Put ``store`` / ``clock`` / venue handles onto ``MarketView``.
    Return a view when the book is STALE or AVAILABILITY_UNKNOWN.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from wxmm.core.errors import LeakageError, StaleBookError
from wxmm.core.types import (
    AsOfRecord,
    BookSnapshot,
    Clock,
    require_clock_bound_book_source,
)
from wxmm.strategy.view import BookView, FillView, MarketView, PositionView


def book_view_from_snapshot(venue: str, snap: BookSnapshot) -> BookView:
    bid = snap.bids[0] if snap.bids else None
    ask = snap.asks[0] if snap.asks else None
    return BookView(
        venue=venue,
        market_id=snap.market_id,
        two_sided=snap.two_sided,
        bid_cents=bid.price_cents if bid is not None else None,
        ask_cents=ask.price_cents if ask is not None else None,
        bid_size=bid.size if bid is not None else None,
        ask_size=ask.size if ask is not None else None,
        ask_size_known=snap.ask_size_known,
        volume=snap.volume,
        reconstructed=snap.reconstructed,
        staleness=snap.staleness,
    )


def book_view_from_record(venue: str, rec: AsOfRecord) -> BookView:
    payload = rec.payload
    if isinstance(payload, BookSnapshot):
        return book_view_from_snapshot(venue, payload)
    if not isinstance(payload, dict):
        raise TypeError(f"cannot build BookView from {type(payload)}")
    bid = payload.get("yes_bid_cents")
    ask = payload.get("yes_ask_cents")
    staleness = payload.get("staleness")
    market_id = payload.get("market_id")
    return BookView(
        venue=venue,
        market_id=str(market_id) if market_id is not None else rec.key,
        two_sided=bool(payload.get("two_sided", False)),
        bid_cents=int(bid) if isinstance(bid, int) else None,
        ask_cents=int(ask) if isinstance(ask, int) else None,
        bid_size=payload.get("bid_size") if isinstance(payload.get("bid_size"), int) else None,
        ask_size=payload.get("ask_size") if isinstance(payload.get("ask_size"), int) else None,
        ask_size_known=payload.get("ask_size") is not None,
        volume=payload.get("volume") if isinstance(payload.get("volume"), int) else None,
        reconstructed=bool(payload.get("reconstructed", False)),
        staleness=staleness if isinstance(staleness, timedelta) else timedelta(0),
    )


def build_market_view(
    *,
    store: object,
    clock: Clock,
    book_keys: Sequence[tuple[str, str]],
    positions: Sequence[PositionView] = (),
    fills: Sequence[FillView] = (),
) -> MarketView:
    """Read books at ``clock.now()``. Future/unknown/stale records raise."""
    bound = require_clock_bound_book_source(store)
    now = clock.now()
    books: list[BookView] = []
    for venue, key in book_keys:
        rec = bound.get(key, as_of=now)
        if rec.availability == "stale":
            raise StaleBookError(f"STALE book cannot enter MarketView key={key!r}")
        if rec.availability != "known":
            raise LeakageError(f"AVAILABILITY_UNKNOWN cannot enter MarketView key={key!r}")
        if rec.available_at > now:
            raise LeakageError(
                f"tainted view: record {key!r} available_at={rec.available_at.isoformat()} "
                f"> clock.now()={now.isoformat()}"
            )
        books.append(book_view_from_record(venue, rec))
    return MarketView(
        books=tuple(books),
        positions=tuple(positions),
        fills=tuple(fills),
    )
