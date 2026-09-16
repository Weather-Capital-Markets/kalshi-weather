"""Pluggable live transports. Emit ``BookUpdate``; never interpret.

Allowed to assume
    Transports parse venue payloads into ``BookUpdate``. Reconnect uses
    exponential backoff and marks every market STALE until a full snapshot.
    Accepted Kalshi shapes: WS/REST ticker quotes, ``orderbook_fp`` ladders,
    and logger JSONL envelopes (``ts_utc`` + ``endpoint`` + ``payload``).
    Polymarket CLOB ``/book`` is not best-first: take max bid / min ask.

Must never
    Build a ``MarketView``. Decide or send orders. Read credentials.
    Import ``wxmm.backtest`` or ``analysis``. Put weather/NBM on a book.
    Convert prices with ``float``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Protocol, cast

from wxmm.core.book import BookUpdate, snapshot_payload, snapshot_update
from wxmm.core.types import Clock, clip_extreme_touch
from wxmm.live.health import FeedHealth
from wxmm.live.state import LiveState


class Transport(Protocol):
    name: str

    def parse(
        self,
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BookUpdate: ...


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
    body, captured_at, captured_market = _unwrap_logger(payload)
    if isinstance(body.get("orderbook_fp"), Mapping):
        return parse_kalshi_rest_book(payload, received_at=received_at, source=source)
    market = _kalshi_market_id(body, captured_market)
    if not market:
        raise ValueError("kalshi ticker payload missing ticker")
    valid_at = _ts(
        body.get("ts") or body.get("valid_at") or body.get("updated_time"),
        captured_at or received_at,
    )
    bid = _cents(body.get("yes_bid_dollars"), body.get("yes_bid_cents"))
    ask = _cents(body.get("yes_ask_dollars"), body.get("yes_ask_cents"))
    bid, ask = clip_extreme_touch(bid, ask)
    bid_size = _qty(body.get("yes_bid_size") or body.get("yes_bid_size_fp") or body.get("bid_size"))
    ask_size = _qty(body.get("yes_ask_size") or body.get("yes_ask_size_fp") or body.get("ask_size"))
    if bid is None:
        bid_size = None
    if ask is None:
        ask_size = None
    return snapshot_update(
        venue="kalshi",
        market_id=market,
        valid_at=valid_at,
        available_at=captured_at or received_at,
        source=source,
        payload=snapshot_payload(
            market_id=market,
            bid_cents=bid,
            ask_cents=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=_qty(body.get("volume") or body.get("volume_fp")),
        ),
    )


def parse_kalshi_rest_book(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    source: str = "kalshi_rest_poll",
    market_id: str | None = None,
) -> BookUpdate:
    """Accept ticker quotes, ``orderbook_fp`` REST bodies, or logger JSONL envelopes."""
    body, captured_at, captured_market = _unwrap_logger(payload)
    book = body.get("orderbook_fp")
    if isinstance(book, Mapping):
        market = market_id or _kalshi_market_id(body, captured_market)
        if not market:
            raise ValueError("kalshi orderbook payload missing ticker")
        bid, bid_size, ask, ask_size = _kalshi_orderbook_top(cast(Mapping[str, object], book))
        bid, ask = clip_extreme_touch(bid, ask)
        if bid is None:
            bid_size = None
        if ask is None:
            ask_size = None
        return snapshot_update(
            venue="kalshi",
            market_id=market,
            valid_at=captured_at or received_at,
            available_at=captured_at or received_at,
            source=source,
            payload=snapshot_payload(
                market_id=market,
                bid_cents=bid,
                ask_cents=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                volume=_qty(body.get("volume") or body.get("volume_fp")),
            ),
        )
    return parse_kalshi_ticker(payload, received_at=received_at, source=source)


def parse_polymarket_clob(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    source: str = "polymarket_clob_poll",
) -> BookUpdate:
    body, captured_at, _captured_market = _unwrap_logger(payload)
    market = str(body.get("market") or body.get("market_id") or body.get("asset_id") or "")
    if not market:
        raise ValueError("polymarket book payload missing market id")
    valid_at = _ts(body.get("timestamp") or body.get("valid_at"), captured_at or received_at)
    # CLOB /book is not best-first: bids ascend, asks descend. Take max bid / min ask.
    bid_cents, bid_size = _best_bid_from_levels(body.get("bids"))
    ask_cents, ask_size = _best_ask_from_levels(body.get("asks"))
    return snapshot_update(
        venue="polymarket",
        market_id=market,
        valid_at=valid_at,
        available_at=captured_at or received_at,
        source=source,
        payload=snapshot_payload(
            market_id=market,
            bid_cents=bid_cents,
            ask_cents=ask_cents,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=_qty(body.get("volume")),
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


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS_UNIX_THRESHOLD = 10**12


def _ts(raw: object, fallback: datetime) -> datetime:
    parsed = _parse_ts(raw)
    return parsed if parsed is not None else fallback


def _parse_ts(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return _unix_to_datetime(raw)
    if isinstance(raw, str) and raw:
        if raw.isdigit():
            return _unix_to_datetime(int(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _unix_to_datetime(raw: int) -> datetime | None:
    if raw < 0:
        return None
    if raw >= _MS_UNIX_THRESHOLD:
        return _UNIX_EPOCH + timedelta(milliseconds=raw)
    return _UNIX_EPOCH + timedelta(seconds=raw)


def _unwrap_logger(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], datetime | None, str | None]:
    """Logger JSONL: ``{ts_utc, endpoint, payload}``. Ticker lives in ``endpoint``."""
    inner = payload.get("payload")
    ts_utc = payload.get("ts_utc")
    endpoint = payload.get("endpoint")
    if isinstance(inner, Mapping) and ts_utc and endpoint is not None:
        return (
            cast(Mapping[str, object], inner),
            _parse_ts(ts_utc),
            _market_from_endpoint(endpoint),
        )
    return payload, None, None


def _market_from_endpoint(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    parts = [part for part in raw.split("/") if part]
    if len(parts) >= 2 and parts[0] == "markets":
        return parts[1]
    return None


def _kalshi_market_id(body: Mapping[str, object], captured_market: str | None) -> str:
    raw = (
        body.get("ticker") or body.get("market_ticker") or body.get("market_id") or captured_market
    )
    return str(raw) if raw else ""


def _kalshi_orderbook_top(
    book: Mapping[str, object],
) -> tuple[int | None, int | None, int | None, int | None]:
    """YES bids in ``yes_dollars``; YES ask = 1 − best NO bid in ``no_dollars``."""
    bid_cents, bid_size = _best_bid_from_levels(book.get("yes_dollars"))
    no_cents, no_size = _best_bid_from_levels(book.get("no_dollars"))
    if no_cents is None:
        return bid_cents, bid_size, None, None
    return bid_cents, bid_size, 100 - no_cents, no_size


def _dollars_to_cents(raw: object) -> int | None:
    """Convert a dollar price to integer cents via Decimal. Never float."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        cents = (Decimal(str(raw)) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        return None
    return int(cents)


def _cents(dollars: object, cents: object) -> int | None:
    if cents is not None and cents != "":
        if isinstance(cents, bool):
            raise TypeError("cents must not be bool")
        if isinstance(cents, int):
            return cents
        try:
            return int(Decimal(str(cents)))
        except InvalidOperation:
            return None
    return _dollars_to_cents(dollars)


def _qty(raw: object) -> int | None:
    """Floor a size to a non-negative int. Fractional contracts ROUND_DOWN."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        parsed = Decimal(str(raw))
    except InvalidOperation:
        return None
    if parsed < 0:
        return None
    return int(parsed.to_integral_value(rounding=ROUND_DOWN))


def _price_to_cents(price: object) -> int | None:
    if price is None or price == "" or isinstance(price, bool):
        return None
    if isinstance(price, int):
        return price
    return _dollars_to_cents(price)


def _price_size(level: object) -> tuple[int, int | None] | None:
    if isinstance(level, Mapping):
        price: object = level.get("price")
        size: object = level.get("size")
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price, size = level[0], level[1]
    else:
        return None
    cents = _price_to_cents(price)
    if cents is None:
        return None
    return cents, _qty(size)


def _iter_levels(levels: object) -> tuple[tuple[int, int | None], ...]:
    if not isinstance(levels, (list, tuple)):
        return ()
    parsed: list[tuple[int, int | None]] = []
    for level in levels:
        row = _price_size(level)
        if row is not None:
            parsed.append(row)
    return tuple(parsed)


def _best_bid_from_levels(levels: object) -> tuple[int | None, int | None]:
    parsed = _iter_levels(levels)
    if not parsed:
        return None, None
    cents, size = max(parsed, key=lambda row: row[0])
    return cents, size


def _best_ask_from_levels(levels: object) -> tuple[int | None, int | None]:
    parsed = _iter_levels(levels)
    if not parsed:
        return None, None
    cents, size = min(parsed, key=lambda row: row[0])
    return cents, size
