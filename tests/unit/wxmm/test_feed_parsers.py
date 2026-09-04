"""Feed parsers emit BookUpdate and never interpret."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wxmm.live.feed import (
    KalshiRestPollTransport,
    KalshiWsTransport,
    PolymarketClobPollTransport,
    next_backoff,
    parse_kalshi_rest_book,
    parse_kalshi_ticker,
    parse_polymarket_clob,
)

UTC = timezone.utc
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "live"


def test_parse_kalshi_ws_and_rest_fixtures() -> None:
    received = datetime(2026, 7, 4, 16, 0, 1, tzinfo=UTC)
    ws = json.loads((FIX / "kalshi_ws_ticker.json").read_text())
    rest = json.loads((FIX / "kalshi_rest_poll.json").read_text())
    u1 = parse_kalshi_ticker(ws, received_at=received)
    u2 = parse_kalshi_rest_book(rest, received_at=received)
    assert u1.venue == "kalshi" and not u1.stale
    assert u1.available_at == received
    payload = u1.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 40
    assert isinstance(u2.payload, dict)
    assert u2.payload["yes_bid_cents"] == 41
    assert KalshiWsTransport().parse(ws, received_at=received).key == u1.key
    assert KalshiRestPollTransport().parse(rest, received_at=received).source == "kalshi_rest_poll"


def test_parse_polymarket_clob_fixture() -> None:
    received = datetime(2026, 7, 4, 16, 0, 1, tzinfo=UTC)
    raw = json.loads((FIX / "polymarket_clob_poll.json").read_text())
    update = parse_polymarket_clob(raw, received_at=received)
    assert update.venue == "polymarket"
    payload = update.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 39
    assert PolymarketClobPollTransport().parse(raw, received_at=received).market_id == "nyc-76-77"


def test_backoff_doubles_and_caps() -> None:
    assert next_backoff(0) == 1.0
    assert next_backoff(1) == 2.0
    assert next_backoff(10, cap=60.0) == 60.0
