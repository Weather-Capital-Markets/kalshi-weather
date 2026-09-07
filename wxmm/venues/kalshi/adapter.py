"""In-memory Kalshi venue adapter. No HTTP; ingestion owns the wire."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from wxmm.core.money import Money
from wxmm.core.types import (
    AsOfRecord,
    AsOfStore,
    BookLevel,
    BookSnapshot,
    Order,
    Trade,
    clip_extreme_touch,
)
from wxmm.core.utc import require_utc
from wxmm.settlement.eras import settlement_rule_in_force
from wxmm.settlement.rules import SettlementRule
from wxmm.venues.kalshi.fees import order_fee

# Basic-tier write budget ≈ 100 tokens/s ≈ 10 orders/s; batch cancel = 2 tokens.
KALSHI_RATE_LIMITS: dict[str, float] = {
    "write_tokens_per_sec": 100.0,
    "orders_per_sec_approx": 10.0,
    "batch_cancel_tokens": 2.0,
}


def empty_book_from_extreme_quotes(bid_cents: int, ask_cents: int) -> bool:
    """venue-facts §1.4: bid 0 / ask 100 is absence, not a 99-cent spread."""
    return bid_cents <= 0 and ask_cents >= 100


class KalshiVenue:
    name = "kalshi"

    def __init__(self, store: AsOfStore) -> None:
        self._store = store

    def list_markets(self, as_of: datetime) -> tuple[str, ...]:
        rec = _optional_get(self._store, "kalshi:markets", as_of)
        if rec is None:
            return ()
        payload = rec.payload
        if isinstance(payload, (list, tuple)):
            return tuple(str(item) for item in payload)
        return ()

    def book_at(self, market: str, as_of: datetime) -> BookSnapshot:
        rec = self._store.get(f"kalshi:book:{market}", as_of)
        if isinstance(rec.payload, BookSnapshot):
            snap = rec.payload
            return _normalize_empty_book(snap)
        return _book_from_payload(market, rec)

    def trades(
        self,
        market: str,
        window: tuple[datetime, datetime],
        as_of: datetime,
    ) -> tuple[Trade, ...]:
        start, end = require_utc(window[0]), require_utc(window[1])
        rec = _optional_get(self._store, f"kalshi:trades:{market}", as_of)
        if rec is None:
            return ()
        payload = rec.payload
        if not isinstance(payload, (list, tuple)):
            return ()
        out: list[Trade] = []
        for item in payload:
            trade = cast(Trade, item)
            if start <= trade.ts < end and trade.available_at <= require_utc(as_of):
                out.append(trade)
        return tuple(out)

    def fee(self, order: Order) -> Money:
        return order_fee(order)

    def rate_limits(self) -> dict[str, float]:
        return dict(KALSHI_RATE_LIMITS)

    def settlement_rule(self, as_of: datetime) -> SettlementRule:
        return settlement_rule_in_force("kalshi", as_of)


def _optional_get(store: AsOfStore, key: str, as_of: datetime) -> AsOfRecord | None:
    from wxmm.core.errors import MissingDataError

    try:
        return store.get(key, as_of)
    except MissingDataError:
        return None


def _normalize_empty_book(snap: BookSnapshot) -> BookSnapshot:
    if not snap.bids and not snap.asks:
        return snap
    best_bid = snap.bids[0].price_cents if snap.bids else 0
    best_ask = snap.asks[0].price_cents if snap.asks else 100
    if empty_book_from_extreme_quotes(best_bid, best_ask):
        return BookSnapshot(
            market_id=snap.market_id,
            valid_at=snap.valid_at,
            available_at=snap.available_at,
            bids=(),
            asks=(),
            volume=snap.volume,
            ask_size_known=snap.ask_size_known,
            reconstructed=snap.reconstructed,
            staleness=snap.staleness,
            two_sided=False,
            source=snap.source,
        )
    return snap


def _optional_int_cents(payload: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def _book_from_payload(market: str, rec: AsOfRecord) -> BookSnapshot:
    payload = rec.payload
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected book payload for {market}")
    bid, ask = clip_extreme_touch(
        _optional_int_cents(payload, "yes_bid_cents", "bid_cents"),
        _optional_int_cents(payload, "yes_ask_cents", "ask_cents"),
    )
    two_sided = bid is not None and ask is not None
    bid_size = payload.get("bid_size")
    ask_size = payload.get("ask_size")
    bid_qty = bid_size if isinstance(bid_size, int) else None
    ask_qty = ask_size if isinstance(ask_size, int) else None
    bids = (BookLevel(bid, bid_qty),) if bid is not None else ()
    asks = (BookLevel(ask, ask_qty),) if ask is not None else ()
    volume = payload.get("volume")
    return BookSnapshot(
        market_id=market,
        valid_at=rec.valid_at,
        available_at=rec.available_at,
        bids=bids,
        asks=asks,
        volume=volume if volume is None or isinstance(volume, int) else None,
        ask_size_known=ask is not None and ask_size is not None,
        reconstructed=bool(payload.get("reconstructed", False)),
        staleness=payload.get("staleness") or timedelta(0),
        two_sided=two_sided,
        source=rec.source,
    )
