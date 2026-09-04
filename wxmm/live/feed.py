"""Pluggable live transports. Emit ``BookUpdate``; never interpret.

Allowed to assume
    Transports parse venue payloads into ``BookUpdate``. Reconnect uses
    exponential backoff and marks every market STALE until a full snapshot.

Must never
    Build a ``MarketView``. Decide or send orders. Read credentials.
    Import ``wxmm.backtest``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from wxmm.core.types import Clock
from wxmm.live.health import FeedHealth
from wxmm.live.state import (
    BookUpdate,
    LiveState,
    snapshot_payload,
    snapshot_update,
)


class Transport(Protocol):
    name: str

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate:
        ...


def next_backoff(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    delay = base * (2**attempt)
    return cap if delay > cap else delay


def parse_kalshi_ticker(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    source: str = "kalshi_ws_ticker",
) -> BookUpdate:
    market = str(payload.get("market_ticker") or payload.get("market_id") or "")
    valid_at = _ts(payload.get("ts") or payload.get("valid_at"), received_at)
    bid = _cents(payload.get("yes_bid_dollars"), payload.get("yes_bid_cents"))
    ask = _cents(payload.get("yes_ask_dollars"), payload.get("yes_ask_cents"))
    return snapshot_update(
        venue="kalshi",
        market_id=market,
        valid_at=valid_at,
        available_at=received_at,
        source=source,
        payload=snapshot_payload(
            market_id=market,
            bid_cents=bid,
            ask_cents=ask,
            bid_size=_int(payload.get("yes_bid_size") or payload.get("bid_size")),
            ask_size=_int(payload.get("yes_ask_size") or payload.get("ask_size")),
            volume=_int(payload.get("volume")),
        ),
    )


def parse_kalshi_rest_book(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    source: str = "kalshi_rest_poll",
) -> BookUpdate:
    return parse_kalshi_ticker(payload, received_at=received_at, source=source)


def parse_polymarket_clob(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    source: str = "polymarket_clob_poll",
) -> BookUpdate:
    market = str(payload.get("market") or payload.get("market_id") or "")
    valid_at = _ts(payload.get("timestamp") or payload.get("valid_at"), received_at)
    bids = payload.get("bids")
    asks = payload.get("asks")
    bid_cents, bid_size = _best_level(bids)
    ask_cents, ask_size = _best_level(asks)
    return snapshot_update(
        venue="polymarket",
        market_id=market,
        valid_at=valid_at,
        available_at=received_at,
        source=source,
        payload=snapshot_payload(
            market_id=market,
            bid_cents=bid_cents,
            ask_cents=ask_cents,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=_int(payload.get("volume")),
        ),
    )


class KalshiWsTransport:
    name = "kalshi_ws"

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate:
        return parse_kalshi_ticker(payload, received_at=received_at)


class KalshiRestPollTransport:
    name = "kalshi_rest"

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate:
        return parse_kalshi_rest_book(payload, received_at=received_at)


class PolymarketClobPollTransport:
    name = "polymarket_clob"

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate:
        return parse_polymarket_clob(payload, received_at=received_at)


class FakeTransport:
    """Yields a recorded tape. Optional disconnect after ``disconnect_after`` items."""

    name = "fake"

    def __init__(
        self,
        updates: Sequence[BookUpdate],
        *,
        disconnect_after: int | None = None,
    ) -> None:
        self._updates = tuple(updates)
        self.disconnect_after = disconnect_after

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate:
        raise TypeError("FakeTransport emits recorded BookUpdates; it does not parse")

    async def updates(self) -> AsyncIterator[BookUpdate]:
        for i, update in enumerate(self._updates):
            if self.disconnect_after is not None and i >= self.disconnect_after:
                raise ConnectionError("fake transport disconnect mid-update")
            yield update
            await asyncio.sleep(0)


class Feed:
    """Pushes transport updates into ``LiveState``. Reconnect marks all STALE."""

    def __init__(
        self,
        state: LiveState,
        health: FeedHealth,
        transport: Transport,
        *,
        clock: Clock,
    ) -> None:
        self.state = state
        self.health = health
        self.transport = transport
        self.clock = clock
        self._reconnects = 0

    def ingest(self, update: BookUpdate) -> None:
        self.state.apply(update)
        self.health.note_update(self.clock.now(), seq=update.seq)

    def note_disconnect(self) -> None:
        self._reconnects += 1
        self.state.mark_all_stale(source=f"reconnect:{self.transport.name}")
        self.health.note_reconnect(self.clock.now())

    async def run_once(self, stream: AsyncIterator[BookUpdate]) -> None:
        try:
            async for update in stream:
                self.ingest(update)
        except ConnectionError:
            self.note_disconnect()
            raise


def _ts(raw: object, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return fallback


def _cents(dollars: object, cents: object) -> int | None:
    if isinstance(cents, int):
        return cents
    if isinstance(dollars, (int, float, str)):
        return int(round(float(dollars) * 100))
    return None


def _int(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _best_level(levels: object) -> tuple[int | None, int | None]:
    if not isinstance(levels, (list, tuple)) or not levels:
        return None, None
    first = levels[0]
    if isinstance(first, Mapping):
        price = first.get("price")
        size = first.get("size")
        cents: int | None
        if isinstance(price, str) and "." in price:
            cents = int(round(float(price) * 100))
        elif isinstance(price, (int, float)):
            cents = int(round(float(price) * 100)) if float(price) <= 1 else int(price)
        else:
            cents = None
        qty = None
        if isinstance(size, int):
            qty = size
        elif isinstance(size, str):
            try:
                qty = int(float(size))
            except ValueError:
                qty = None
        return cents, qty
    return None, None
