"""Shared live/replay book tape. ``BookUpdate`` is the only tape record.

Allowed to assume
    Live and replay consume the same frozen objects, including ``valid_at``,
    ``available_at``, and the STALE flag. Keys include ``Underlying`` when
    the caller knows it, plus venue market id.

Must never
    Live-only fields. Interpret trading meaning. Call ``datetime.now``.
    Import ``wxmm.live`` or ``wxmm.backtest``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from wxmm.core.types import (
    AsOfRecord,
    AsOfStore,
    BookSnapshot,
    clip_extreme_touch,
    received_record,
)
from wxmm.core.underlying import Underlying
from wxmm.core.utc import require_utc


def book_store_key(
    venue: str,
    market_id: str,
    underlying: Underlying | None = None,
) -> str:
    """Books are keyed by Underlying (when known) plus venue market id."""
    if underlying is None:
        return f"{venue}:book:{market_id}"
    return (
        f"{venue}:book:{market_id}:"
        f"{underlying.station}:{underlying.product}:"
        f"{underlying.day_convention}:{underlying.revision_rule}"
    )


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
        key=key or book_store_key(venue, market_id, underlying),
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
        key=key or book_store_key(venue, market_id, underlying),
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
