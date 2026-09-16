"""Live venue payloads must enter the same MarketView path as replay.

Research capture (2026-09-04 UTC) of Kalshi KXHIGHNY-26SEP04-B84.5 and
Polymarket KLGA Sep 4 84–85°F: parsers ingest logger/REST/CLOB shapes,
LiveState and ClockBoundStore produce identical views, and propose()
consumes that view. Weather/NBM is not on MarketView.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from wxmm.core.book import apply_book_update
from wxmm.core.types import ClockBoundStore, FrozenClock, InMemoryAsOfStore
from wxmm.core.view import build_market_view
from wxmm.decide.engine import propose
from wxmm.decide.fairvalue import NullFairValue
from wxmm.live.feed import Feed, KalshiWsTransport, parse_kalshi_ticker, parse_polymarket_clob
from wxmm.live.health import FeedHealth
from wxmm.live.state import LiveState
from wxmm.strategy.view import MarketView

UTC = timezone.utc
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "live"
RECEIVED = datetime(2026, 9, 4, 3, 5, tzinfo=UTC)
LOGGER_TS = datetime(2026, 9, 4, 3, 1, tzinfo=UTC)
PM_TS = datetime(2026, 9, 4, 3, 3, 6, 713000, tzinfo=UTC)


def _load(name: str) -> dict[str, object]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_live_and_replay_views_match_for_captured_tape() -> None:
    kalshi = parse_kalshi_ticker(_load("kalshi_logger_orderbook.json"), received_at=RECEIVED)
    poly = parse_polymarket_clob(_load("polymarket_clob_book.json"), received_at=RECEIVED)
    clock = FrozenClock(RECEIVED)
    live = LiveState(clock)
    live.apply(kalshi)
    live.apply(poly)
    replay = ClockBoundStore(InMemoryAsOfStore(), clock)
    apply_book_update(replay, kalshi)
    apply_book_update(replay, poly)
    book_keys = (("kalshi", kalshi.key), ("polymarket", poly.key))
    live_view = build_market_view(store=live, clock=clock, book_keys=book_keys)
    replay_view = build_market_view(store=replay, clock=clock, book_keys=book_keys)
    assert live_view == replay_view
    assert live_view.books[0].bid_cents == 52
    assert live_view.books[0].ask_cents == 55
    assert live_view.books[1].bid_cents == 40
    assert live_view.books[1].ask_cents == 41


def test_propose_consumes_live_captured_view_without_weather_on_view() -> None:
    kalshi = parse_kalshi_ticker(_load("kalshi_logger_orderbook.json"), received_at=RECEIVED)
    clock = FrozenClock(RECEIVED)
    live = LiveState(clock)
    live.apply(kalshi)
    view = build_market_view(store=live, clock=clock, book_keys=(("kalshi", kalshi.key),))
    assert {f.name for f in fields(MarketView)} == {"books", "positions", "fills"}
    assert not hasattr(view, "nbm")
    assert not hasattr(view, "weather")
    assert NullFairValue().fair(view) is None
    out = propose(view)
    assert {(p.side, p.price) for p in out} == {("buy", 52), ("sell", 55)}
    assert all(p.edge is None for p in out)
    assert all("no fair value" in p.rationale for p in out)


def test_feed_ingest_live_logger_then_view() -> None:
    clock = FrozenClock(RECEIVED)
    state = LiveState(clock)
    feed = Feed(state, FeedHealth("kalshi"), KalshiWsTransport(), clock=clock)
    update = KalshiWsTransport().parse(_load("kalshi_logger_orderbook.json"), received_at=RECEIVED)
    feed.ingest(update)
    view = build_market_view(store=state, clock=clock, book_keys=(("kalshi", update.key),))
    book = view.books[0]
    assert book.market_id == "KXHIGHNY-26SEP04-B84.5"
    assert book.bid_cents == 52
    assert book.ask_cents == 55
    assert book.bid_size == 28
    assert book.ask_size == 6
    assert book.two_sided is True
    assert update.valid_at == LOGGER_TS
    assert update.available_at == LOGGER_TS


def test_polymarket_valid_at_is_unix_ms_not_first_level() -> None:
    raw = _load("polymarket_clob_book.json")
    assert isinstance(raw["bids"], list)
    first_bid = raw["bids"][0]
    assert isinstance(first_bid, dict)
    assert first_bid["price"] == "0.01"
    update = parse_polymarket_clob(raw, received_at=RECEIVED)
    payload = update.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 40
    assert payload["yes_ask_cents"] == 41
    assert update.valid_at == PM_TS
