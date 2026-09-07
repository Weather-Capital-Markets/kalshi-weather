"""BookSource identity: Underlying + venue market id."""

from __future__ import annotations

from datetime import datetime, timezone

from wxmm.core.book import book_store_key, snapshot_payload, snapshot_update
from wxmm.core.types import FrozenClock
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.core.view import build_market_view
from wxmm.live.state import LiveState

UTC = timezone.utc
TS = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def test_same_market_id_different_underlyings_do_not_share_a_slot() -> None:
    knyc = snapshot_update(
        venue="kalshi",
        market_id="M",
        valid_at=TS,
        available_at=TS,
        payload=snapshot_payload(
            market_id="M", bid_cents=40, ask_cents=42, bid_size=5, ask_size=5
        ),
        source="t",
        underlying=KALSHI_NYC_DAILY_HIGH,
    )
    klga = snapshot_update(
        venue="kalshi",
        market_id="M",
        valid_at=TS,
        available_at=TS,
        payload=snapshot_payload(
            market_id="M", bid_cents=10, ask_cents=12, bid_size=1, ask_size=1
        ),
        source="t",
        underlying=POLYMARKET_NYC_DAILY_HIGH,
    )
    assert knyc.key != klga.key
    clock = FrozenClock(TS)
    live = LiveState(clock)
    live.apply(knyc)
    live.apply(klga)
    a = live.get_identity(KALSHI_NYC_DAILY_HIGH, "kalshi", "M", as_of=TS)
    b = live.get_identity(POLYMARKET_NYC_DAILY_HIGH, "kalshi", "M", as_of=TS)
    payload_a = a.payload
    payload_b = b.payload
    assert isinstance(payload_a, dict) and isinstance(payload_b, dict)
    assert payload_a["yes_bid_cents"] == 40
    assert payload_b["yes_bid_cents"] == 10
    view = build_market_view(
        store=live,
        clock=clock,
        book_keys=(("kalshi", knyc.key), ("kalshi", klga.key)),
    )
    assert view.books[0].bid_cents == 40
    assert view.books[1].bid_cents == 10


def test_book_store_key_without_underlying_stays_venue_market() -> None:
    assert book_store_key("kalshi", "M") == "kalshi:book:M"
    keyed = book_store_key("kalshi", "M", KALSHI_NYC_DAILY_HIGH)
    assert keyed != book_store_key("kalshi", "M")
    assert "KNYC" in keyed
