"""In-memory Polymarket venue adapter. Fee schedule is a hard failure."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from wxmm.core.errors import UnverifiedFeeSchedule
from wxmm.core.money import Money
from wxmm.core.types import (
    POLYMARKET_FEE_SCHEDULE,
    AsOfRecord,
    AsOfStore,
    BookLevel,
    BookSnapshot,
    Order,
    Trade,
)
from wxmm.core.utc import require_utc
from wxmm.settlement.eras import settlement_rule_in_force
from wxmm.settlement.rules import SettlementRule

POLYMARKET_RATE_LIMITS: dict[str, float] = {
    "requests_per_sec_per_ip": 20.0,
}


class PolymarketVenue:
    name = "polymarket"

    def __init__(self, store: AsOfStore) -> None:
        self._store = store

    def list_markets(self, as_of: datetime) -> tuple[str, ...]:
        rec = _optional_get(self._store, "polymarket:markets", as_of)
        if rec is None:
            return ()
        payload = rec.payload
        if isinstance(payload, (list, tuple)):
            return tuple(str(item) for item in payload)
        return ()

    def book_at(self, market: str, as_of: datetime) -> BookSnapshot:
        rec = self._store.get(f"polymarket:book:{market}", as_of)
        if isinstance(rec.payload, BookSnapshot):
            return rec.payload
        return _book_from_payload(market, rec)

    def trades(
        self,
        market: str,
        window: tuple[datetime, datetime],
        as_of: datetime,
    ) -> tuple[Trade, ...]:
        start, end = require_utc(window[0]), require_utc(window[1])
        rec = _optional_get(self._store, f"polymarket:trades:{market}", as_of)
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
        raise UnverifiedFeeSchedule(
            f"refusing to price Polymarket order {order.market_id!r}: "
            f"{POLYMARKET_FEE_SCHEDULE.name} is {POLYMARKET_FEE_SCHEDULE.status.value} "
            f"({POLYMARKET_FEE_SCHEDULE.source}). Verify with date+source before pricing."
        )

    def rate_limits(self) -> dict[str, float]:
        return dict(POLYMARKET_RATE_LIMITS)

    def settlement_rule(self, as_of: datetime) -> SettlementRule:
        return settlement_rule_in_force("polymarket", as_of)


def _optional_get(store: AsOfStore, key: str, as_of: datetime) -> AsOfRecord | None:
    from wxmm.core.errors import MissingDataError

    try:
        return store.get(key, as_of)
    except MissingDataError:
        return None


def _book_from_payload(market: str, rec: AsOfRecord) -> BookSnapshot:
    payload = rec.payload
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected book payload for {market}")
    bid = int(payload.get("bid_cents", 0))
    ask = int(payload.get("ask_cents", 100))
    two_sided = 0 < bid < ask < 100
    bids = (BookLevel(bid, payload.get("bid_size")),) if two_sided else ()
    asks = (BookLevel(ask, payload.get("ask_size")),) if two_sided else ()
    ask_size = payload.get("ask_size")
    volume = payload.get("volume")
    return BookSnapshot(
        market_id=market,
        valid_at=rec.valid_at,
        available_at=rec.available_at,
        bids=bids,
        asks=asks,
        volume=volume if volume is None or isinstance(volume, int) else int(volume),
        ask_size_known=ask_size is not None,
        reconstructed=bool(payload.get("reconstructed", False)),
        staleness=payload.get("staleness") or timedelta(0),
        two_sided=two_sided,
        source=rec.source,
    )
