"""Feed parsers emit BookUpdate and never interpret."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from wxmm.live import feed as feed_mod
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


def test_dollar_prices_convert_via_decimal() -> None:
    received = datetime(2026, 7, 4, 16, 0, 1, tzinfo=UTC)
    via_str = parse_kalshi_ticker(
        {
            "market_ticker": "M",
            "yes_bid_dollars": "0.29",
            "yes_ask_dollars": "0.31",
        },
        received_at=received,
    )
    payload = via_str.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 29
    assert payload["yes_ask_cents"] == 31
    via_float = parse_kalshi_ticker(
        {
            "market_ticker": "M",
            "yes_bid_dollars": 0.40,
            "yes_ask_dollars": 0.42,
        },
        received_at=received,
    )
    payload_f = via_float.payload
    assert isinstance(payload_f, dict)
    assert payload_f["yes_bid_cents"] == 40
    assert "float(" not in inspect.getsource(feed_mod._cents)
    assert "float(" not in inspect.getsource(feed_mod._dollars_to_cents)
    assert "float(" not in inspect.getsource(feed_mod._qty)
    assert "float(" not in inspect.getsource(feed_mod._best_bid_from_levels)
    assert "float(" not in inspect.getsource(feed_mod._kalshi_orderbook_top)
    src = Path(feed_mod.__file__).read_text(encoding="utf-8")
    assert "from analysis" not in src
    assert "import analysis" not in src


def test_live_kalshi_logger_orderbook_yes_no_ladder() -> None:
    received = datetime(2026, 9, 4, 3, 5, tzinfo=UTC)
    payload = json.loads((FIX / "kalshi_logger_orderbook.json").read_text())
    update = parse_kalshi_ticker(payload, received_at=received)
    body = update.payload
    assert isinstance(body, dict)
    assert update.market_id == "KXHIGHNY-26SEP04-B84.5"
    assert body["yes_bid_cents"] == 52
    assert body["yes_ask_cents"] == 55
    assert body["bid_size"] == 28
    assert body["ask_size"] == 6
    assert update.valid_at == datetime(2026, 9, 4, 3, 1, tzinfo=UTC)


def test_live_kalshi_markets_ticker_uses_ticker_not_market_ticker() -> None:
    received = datetime(2026, 9, 4, 3, 5, tzinfo=UTC)
    payload = json.loads((FIX / "kalshi_market_ticker.json").read_text())
    update = parse_kalshi_ticker(payload, received_at=received)
    body = update.payload
    assert isinstance(body, dict)
    assert update.market_id == "KXHIGHNY-26SEP04-B84.5"
    assert body["yes_bid_cents"] == 52
    assert body["yes_ask_cents"] == 55
    assert body["bid_size"] == 28
    assert body["ask_size"] == 6


def test_kalshi_rest_orderbook_fp_without_logger_envelope() -> None:
    received = datetime(2026, 9, 4, 3, 5, tzinfo=UTC)
    envelope = json.loads((FIX / "kalshi_logger_orderbook.json").read_text())
    body = envelope["payload"]
    update = parse_kalshi_rest_book(
        body,
        received_at=received,
        market_id="KXHIGHNY-26SEP04-B84.5",
    )
    payload = update.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 52
    assert payload["yes_ask_cents"] == 55


def test_live_polymarket_clob_best_is_not_first_level() -> None:
    received = datetime(2026, 9, 4, 3, 5, tzinfo=UTC)
    raw = json.loads((FIX / "polymarket_clob_book.json").read_text())
    update = parse_polymarket_clob(raw, received_at=received)
    payload = update.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] == 40
    assert payload["yes_ask_cents"] == 41
    assert payload["bid_size"] == 1655
    assert payload["ask_size"] == 299
    assert update.valid_at == datetime(2026, 9, 4, 3, 3, 6, 713000, tzinfo=UTC)


def test_kalshi_empty_book_extremes_are_clipped() -> None:
    received = datetime(2026, 7, 4, 16, 0, 1, tzinfo=UTC)
    update = parse_kalshi_ticker(
        {
            "market_ticker": "M",
            "yes_bid_cents": 0,
            "yes_ask_cents": 100,
            "yes_bid_size": 3,
            "yes_ask_size": 4,
        },
        received_at=received,
    )
    payload = update.payload
    assert isinstance(payload, dict)
    assert payload["yes_bid_cents"] is None
    assert payload["yes_ask_cents"] is None
    assert payload["two_sided"] is False
    assert payload["bid_size"] is None
    assert payload["ask_size"] is None
