"""Single ``MarketView`` constructor. Live and replay both call this.

Allowed to assume
    The store is a clock-bound ``BookSource`` (``ClockBoundStore`` or
    ``LiveState``). Records have already been filtered by ``available_at``.

Must never
    Build a live-only view. Import ``wxmm.live`` or ``wxmm.backtest``.
    Put ``store`` / ``clock`` / venue handles onto ``MarketView``.
    Return a view when the book is STALE or AVAILABILITY_UNKNOWN.
    Treat Kalshi 0/100 quotes as a two-sided book.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from wxmm.core.errors import LeakageError, StaleBookError
from wxmm.core.types import (
    AsOfRecord,
    BookSnapshot,
    Clock,
    clip_extreme_touch,
    require_clock_bound_book_source,
)
from wxmm.strategy.view import BookView, FillView, MarketView, PositionView


def book_view_from_snapshot(venue: str, snap: BookSnapshot) -> BookView:
    bid_lvl = snap.bids[0] if snap.bids else None
    ask_lvl = snap.asks[0] if snap.asks else None
    bid_cents, ask_cents = clip_extreme_touch(
        bid_lvl.price_cents if bid_lvl is not None else None,
        ask_lvl.price_cents if ask_lvl is not None else None,
    )
    two_sided = bid_cents is not None and ask_cents is not None
    return BookView(
        venue=venue,
        market_id=snap.market_id,
        two_sided=two_sided,
        bid_cents=bid_cents,
        ask_cents=ask_cents,
        bid_size=bid_lvl.size if bid_lvl is not None and bid_cents is not None else None,
        ask_size=ask_lvl.size if ask_lvl is not None and ask_cents is not None else None,
        ask_size_known=snap.ask_size_known if ask_cents is not None else False,
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
    bid_raw = payload.get("yes_bid_cents")
    ask_raw = payload.get("yes_ask_cents")
    bid = bid_raw if isinstance(bid_raw, int) and not isinstance(bid_raw, bool) else None
    ask = ask_raw if isinstance(ask_raw, int) and not isinstance(ask_raw, bool) else None
    bid, ask = clip_extreme_touch(bid, ask)
    two_sided = bid is not None and ask is not None
    bid_size = payload.get("bid_size") if isinstance(payload.get("bid_size"), int) else None
    ask_size = payload.get("ask_size") if isinstance(payload.get("ask_size"), int) else None
    if bid is None:
        bid_size = None
    if ask is None:
        ask_size = None
    staleness = payload.get("staleness")
    market_id = payload.get("market_id")
    return BookView(
        venue=venue,
        market_id=str(market_id) if market_id is not None else rec.key,
        two_sided=two_sided,
        bid_cents=bid,
        ask_cents=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        ask_size_known=ask_size is not None,
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
