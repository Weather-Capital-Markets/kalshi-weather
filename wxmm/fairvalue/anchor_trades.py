"""Trade-derived YES-space book. Provenance TRADE_DERIVED only.

A trade-derived mid is a worse anchor than a live two-sided quote.

It only updates when someone trades, so it is stale by construction in thin
markets, and thinness correlates with the periods where edge would be largest.
It observes the touch only, never depth, so it says nothing about size —
SIZE_UNKNOWN still propagates. Its bid and ask are drawn from different
instants, so implied_spread is an upper bound on the contemporaneous spread
rather than an estimate of it. When both sides are stale, the mid is a
statement about the past, which is when a model built on it will be most
confidently wrong.

Every one of those is a reason to prefer the reconstructed anchor if Stage 0
validates it. This path exists because Stage 0 might not, and because the
reconstructed-vs-trade comparison is worth having either way.

Allowed to assume
    Canonical taker_outcome_side and taker_book_side are on every non-block
    trade. Direction uses outcome only: yes → d=+1 (YES-space ask print),
    no → d=−1 (YES-space bid print). YES-space price is yes_price
    (= 1 − no_price after complement). The live KXHIGHNY cross-tab
    (2026-09-13 and re-probe 2026-09-15) is the anti-diagonal yes↔bid /
    no↔ask; that bijection is recorded, not used to flip d. created_time
    is the availability clock.

Must never
    Assume taker_book_side's frame without a clean one-to-one cross-tab.
    Pick an interpretation when the tab is dirty. Read the deprecated
    aggressor field. Impute a missing side as 0. Average or clamp a crossed
    book. Satisfy ReconstructionBoundRequired. Return a partial ladder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from wxmm.analysis.trades_ingest import RawTrade, assert_price_complement
from wxmm.core.errors import InconsistentTakerMapping, LeakageError
from wxmm.core.utc import require_utc

PROVENANCE: Literal["TRADE_DERIVED"] = "TRADE_DERIVED"


@dataclass(frozen=True, slots=True)
class ObservedMapping:
    """Empirical taker_outcome_side × taker_book_side table. Not an assumption."""

    counts: dict[tuple[str, str], int]
    outcome_to_book: dict[str, str]
    n_non_block: int
    status: Literal["clean"]


@dataclass(frozen=True, slots=True)
class YesSpacePrint:
    direction: Literal[1, -1]
    yes_price: Decimal
    created_time: datetime
    count: Decimal
    ticker: str


@dataclass(frozen=True, slots=True)
class TradeImpliedBook:
    bid: Decimal | None
    bid_at: datetime | None
    bid_staleness: timedelta | None
    ask: Decimal | None
    ask_at: datetime | None
    ask_staleness: timedelta | None
    mid: Decimal | None
    implied_spread: Decimal | None
    provenance: Literal["TRADE_DERIVED"]
    crossed_invalidations: int
    n_block_excluded: int
    size_unknown: Literal[True] = True


@dataclass(frozen=True, slots=True)
class TradeDerivedLadder:
    books: dict[str, TradeImpliedBook]
    provenance: Literal["TRADE_DERIVED"] = PROVENANCE

    def implied_probs(self) -> dict[str, Decimal | None]:
        return {ticker: book.mid for ticker, book in self.books.items()}


def assert_outcome_bookside_mapping(
    trades: Sequence[RawTrade],
) -> ObservedMapping:
    """Require a clean bijection. Do not pick a frame if the table is dirty."""
    counts: dict[tuple[str, str], int] = {
        ("yes", "ask"): 0,
        ("yes", "bid"): 0,
        ("no", "ask"): 0,
        ("no", "bid"): 0,
    }
    n = 0
    for trade in trades:
        if trade.is_block_trade:
            continue
        n += 1
        key = (trade.taker_outcome_side, trade.taker_book_side)
        if key not in counts:
            raise InconsistentTakerMapping(f"unexpected pair {key}")
        counts[key] += 1
    if n == 0:
        raise InconsistentTakerMapping("no non-block trades to verify mapping")

    yes_ask, yes_bid = counts[("yes", "ask")], counts[("yes", "bid")]
    no_ask, no_bid = counts[("no", "ask")], counts[("no", "bid")]
    if yes_ask and yes_bid:
        raise InconsistentTakerMapping(
            f"yes maps to both ask ({yes_ask}) and bid ({yes_bid}); do not pick a frame"
        )
    if no_ask and no_bid:
        raise InconsistentTakerMapping(
            f"no maps to both ask ({no_ask}) and bid ({no_bid}); do not pick a frame"
        )
    if yes_ask and no_ask:
        raise InconsistentTakerMapping("ask maps to both yes and no")
    if yes_bid and no_bid:
        raise InconsistentTakerMapping("bid maps to both yes and no")

    diagonal = yes_ask > 0 and no_bid > 0 and yes_bid == 0 and no_ask == 0
    anti = yes_bid > 0 and no_ask > 0 and yes_ask == 0 and no_bid == 0
    if not (diagonal or anti):
        raise InconsistentTakerMapping(
            f"cross-tab is not a complete one-to-one bijection: {counts}"
        )
    outcome_to_book = {"yes": "ask", "no": "bid"} if diagonal else {"yes": "bid", "no": "ask"}
    return ObservedMapping(
        counts=counts,
        outcome_to_book=outcome_to_book,
        n_non_block=n,
        status="clean",
    )


def yes_space_print(trade: RawTrade) -> YesSpacePrint:
    """Direction from outcome only. Price is YES-space yes_price."""
    assert_price_complement(trade.yes_price, trade.no_price, trade_id=trade.trade_id)
    complement = Decimal("1") - trade.no_price
    if abs(trade.yes_price - complement) > Decimal("0.0001"):
        raise InconsistentTakerMapping(
            f"yes_price {trade.yes_price} != 1-no_price {complement} on {trade.trade_id}"
        )
    direction: Literal[1, -1] = 1 if trade.taker_outcome_side == "yes" else -1
    return YesSpacePrint(
        direction=direction,
        yes_price=trade.yes_price,
        created_time=require_utc(trade.created_time),
        count=trade.count,
        ticker=trade.ticker,
    )


def _empty_book() -> TradeImpliedBook:
    return TradeImpliedBook(
        bid=None,
        bid_at=None,
        bid_staleness=None,
        ask=None,
        ask_at=None,
        ask_staleness=None,
        mid=None,
        implied_spread=None,
        provenance=PROVENANCE,
        crossed_invalidations=0,
        n_block_excluded=0,
    )


def _with_staleness(book: TradeImpliedBook, as_of: datetime) -> TradeImpliedBook:
    now = require_utc(as_of)
    bid_stale = (now - book.bid_at) if book.bid_at is not None else None
    ask_stale = (now - book.ask_at) if book.ask_at is not None else None
    mid = None
    spread = None
    if book.bid is not None and book.ask is not None:
        mid = (book.bid + book.ask) / Decimal("2")
        spread = book.ask - book.bid
    return TradeImpliedBook(
        bid=book.bid,
        bid_at=book.bid_at,
        bid_staleness=bid_stale,
        ask=book.ask,
        ask_at=book.ask_at,
        ask_staleness=ask_stale,
        mid=mid,
        implied_spread=spread,
        provenance=PROVENANCE,
        crossed_invalidations=book.crossed_invalidations,
        n_block_excluded=book.n_block_excluded,
    )


def _invalidate_older(
    bid: Decimal | None,
    bid_at: datetime | None,
    ask: Decimal | None,
    ask_at: datetime | None,
    crossed: int,
) -> tuple[Decimal | None, datetime | None, Decimal | None, datetime | None, int]:
    if bid is None or ask is None or ask >= bid:
        return bid, bid_at, ask, ask_at, crossed
    # Crossed: invalidate the older observation. Do not average, clamp, or pick fresher.
    crossed += 1
    if bid_at is None or ask_at is None:
        return bid, bid_at, ask, ask_at, crossed
    if bid_at < ask_at:
        return None, None, ask, ask_at, crossed
    if ask_at < bid_at:
        return bid, bid_at, None, None, crossed
    # Equal timestamps: both observations are the same age; drop both rather than pick.
    return None, None, None, None, crossed


def apply_print(
    book: TradeImpliedBook,
    print_: YesSpacePrint,
    *,
    as_of: datetime,
    is_block: bool,
) -> TradeImpliedBook:
    if is_block:
        updated = TradeImpliedBook(
            bid=book.bid,
            bid_at=book.bid_at,
            bid_staleness=book.bid_staleness,
            ask=book.ask,
            ask_at=book.ask_at,
            ask_staleness=book.ask_staleness,
            mid=book.mid,
            implied_spread=book.implied_spread,
            provenance=PROVENANCE,
            crossed_invalidations=book.crossed_invalidations,
            n_block_excluded=book.n_block_excluded + 1,
        )
        return _with_staleness(updated, as_of)

    bid, bid_at, ask, ask_at = book.bid, book.bid_at, book.ask, book.ask_at
    if print_.direction == 1:
        ask, ask_at = print_.yes_price, print_.created_time
    else:
        bid, bid_at = print_.yes_price, print_.created_time
    bid, bid_at, ask, ask_at, crossed = _invalidate_older(
        bid, bid_at, ask, ask_at, book.crossed_invalidations
    )
    updated = TradeImpliedBook(
        bid=bid,
        bid_at=bid_at,
        bid_staleness=None,
        ask=ask,
        ask_at=ask_at,
        ask_staleness=None,
        mid=None,
        implied_spread=None,
        provenance=PROVENANCE,
        crossed_invalidations=crossed,
        n_block_excluded=book.n_block_excluded,
    )
    return _with_staleness(updated, as_of)


def trade_available_at(trade: RawTrade) -> datetime:
    """Fill time is the availability clock. Do not invent wall-clock."""
    return require_utc(trade.created_time)


def filter_trades_as_of(
    trades: Sequence[RawTrade],
    as_of: datetime,
    *,
    ticker: str | None = None,
) -> list[RawTrade]:
    """Keep prints with available_at <= t. Does not impute absence."""
    cutoff = require_utc(as_of)
    out: list[RawTrade] = []
    for trade in trades:
        if ticker is not None and trade.ticker != ticker:
            continue
        if trade_available_at(trade) <= cutoff:
            out.append(trade)
    return out


def refuse_if_leaked(
    trades: Sequence[RawTrade],
    as_of: datetime,
    *,
    ticker: str | None = None,
) -> None:
    """As-of violation: a print after t was passed into an as-of path."""
    cutoff = require_utc(as_of)
    for trade in trades:
        if ticker is not None and trade.ticker != ticker:
            continue
        avail = trade_available_at(trade)
        if avail > cutoff:
            raise LeakageError(
                f"trade {trade.trade_id} available_at={avail.isoformat()} "
                f"after as_of={cutoff.isoformat()}"
            )


def stamp_staleness(book: TradeImpliedBook, as_of: datetime) -> TradeImpliedBook:
    """Recompute per-side age vs the as-of clock. Does not move bid/ask."""
    return _with_staleness(book, as_of)


def trades_at_or_before(
    trades: Sequence[RawTrade],
    as_of: datetime,
) -> list[RawTrade]:
    """Alias of ``filter_trades_as_of`` without a ticker filter."""
    return filter_trades_as_of(trades, as_of)


def implied_book_from_trades(
    trades: Sequence[RawTrade],
    *,
    as_of: datetime,
    ticker: str | None = None,
    mapping: ObservedMapping | None = None,
) -> TradeImpliedBook:
    """Replay prints in time order. Mapping must already be clean."""
    as_of_utc = require_utc(as_of)
    refuse_if_leaked(trades, as_of_utc, ticker=ticker)
    eligible = [t for t in trades if ticker is None or t.ticker == ticker]
    future = [t for t in eligible if require_utc(t.created_time) > as_of_utc]
    if future:
        raise LeakageError(
            f"{len(future)} trade(s) with created_time after as_of "
            f"{as_of_utc.isoformat()}; created_time is available_at for prints"
        )
    non_block = [t for t in eligible if not t.is_block_trade]
    if mapping is None and non_block:
        mapping = assert_outcome_bookside_mapping(non_block)
    _ = mapping  # gate only; direction still uses outcome, not book side
    if not eligible:
        return _with_staleness(_empty_book(), as_of_utc)
    ordered = sorted(eligible, key=lambda t: (t.created_time, t.trade_id))
    book = _empty_book()
    for trade in ordered:
        if trade.is_block_trade:
            dummy = YesSpacePrint(
                direction=1,
                yes_price=trade.yes_price,
                created_time=require_utc(trade.created_time),
                count=trade.count,
                ticker=trade.ticker,
            )
            book = apply_print(book, dummy, as_of=as_of, is_block=True)
            continue
        book = apply_print(book, yes_space_print(trade), as_of=as_of, is_block=False)
    return book


def trade_derived_ladder(
    books: Mapping[str, TradeImpliedBook],
) -> TradeDerivedLadder | None:
    """None for the whole ladder if any bracket mid is missing. Never partial."""
    if not books:
        return None
    if any(book.mid is None for book in books.values()):
        return None
    return TradeDerivedLadder(books=dict(books), provenance=PROVENANCE)
