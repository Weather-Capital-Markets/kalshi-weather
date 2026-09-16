"""Derive top-of-book and depth metrics from Kalshi orderbook payloads."""

from __future__ import annotations

from typing import Any

from ingestion.validate_units import price_as_dollars

CENT = 0.01


def _parse_level(level: Any) -> tuple[float, float] | None:
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return None
    try:
        return float(level[0]), float(level[1])
    except (TypeError, ValueError):
        return None


def parse_yes_bid_levels(orderbook_fp: dict[str, Any]) -> list[tuple[float, float]]:
    """Return (price, size) yes bids sorted descending by price."""
    raw = orderbook_fp.get("yes_dollars")
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for item in raw:
        parsed = _parse_level(item)
        if parsed is not None:
            levels.append(parsed)
    levels.sort(key=lambda row: row[0], reverse=True)
    return levels


def parse_no_bid_levels(orderbook_fp: dict[str, Any]) -> list[tuple[float, float]]:
    """Return (no_price, size) levels sorted descending by no price."""
    raw = orderbook_fp.get("no_dollars")
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for item in raw:
        parsed = _parse_level(item)
        if parsed is not None:
            levels.append(parsed)
    levels.sort(key=lambda row: row[0], reverse=True)
    return levels


def parse_yes_ask_levels(orderbook_fp: dict[str, Any]) -> list[tuple[float, float]]:
    """Return (yes_ask_price, size) sorted ascending by yes ask."""
    asks: list[tuple[float, float]] = []
    for no_px, size in parse_no_bid_levels(orderbook_fp):
        asks.append((1.0 - no_px, size))
    asks.sort(key=lambda row: row[0])
    return asks


def _best_yes_bid(orderbook_fp: dict[str, Any]) -> float | None:
    bids = parse_yes_bid_levels(orderbook_fp)
    return bids[0][0] if bids else None


def _best_yes_ask(orderbook_fp: dict[str, Any]) -> float | None:
    asks = parse_yes_ask_levels(orderbook_fp)
    return asks[0][0] if asks else None


def top_of_book_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (yes_bid, yes_ask) from a logger orderbook API payload."""
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None, None
    return _best_yes_bid(book), _best_yes_ask(book)


def size_at_best_levels(levels: list[tuple[float, float]], n: int) -> float:
    return sum(size for _, size in levels[:n])


def dollar_size(levels: list[tuple[float, float]], n: int | None = None) -> float:
    subset = levels[:n] if n is not None else levels
    return sum(price * size for price, size in subset)


def depth_within_cents(
    mid: float,
    bid_levels: list[tuple[float, float]],
    ask_levels: list[tuple[float, float]],
    cents: int,
) -> float:
    """Sum resting contracts within *cents* of mid on both sides."""
    threshold = cents * CENT
    bid_depth = sum(size for price, size in bid_levels if price >= mid - threshold)
    ask_depth = sum(size for price, size in ask_levels if price <= mid + threshold)
    return bid_depth + ask_depth


def dollar_depth_within_cents(
    mid: float,
    bid_levels: list[tuple[float, float]],
    ask_levels: list[tuple[float, float]],
    cents: int,
) -> float:
    threshold = cents * CENT
    bid_dollars = sum(price * size for price, size in bid_levels if price >= mid - threshold)
    ask_dollars = sum(price * size for price, size in ask_levels if price <= mid + threshold)
    return bid_dollars + ask_dollars


def top_of_book_imbalance(bid_size: float, ask_size: float) -> float | None:
    total = bid_size + ask_size
    if total <= 0:
        return None
    return (bid_size - ask_size) / total


def depth_metrics_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Compute depth statistics from a Kalshi orderbook payload."""
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None
    bid_levels = parse_yes_bid_levels(book)
    ask_levels = parse_yes_ask_levels(book)
    no_levels = parse_no_bid_levels(book)
    bid, ask = _best_yes_bid(book), _best_yes_ask(book)
    if bid is None or ask is None:
        return None
    bid = price_as_dollars(bid, label="yes_bid")
    ask = price_as_dollars(ask, label="yes_ask")
    mid = (bid + ask) / 2.0
    yes_top_bid_size = bid_levels[0][1] if bid_levels else 0.0
    yes_top_ask_size = ask_levels[0][1] if ask_levels else 0.0
    metrics: dict[str, Any] = {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "yes_size_1": size_at_best_levels(bid_levels, 1),
        "yes_size_2": size_at_best_levels(bid_levels, 2),
        "yes_size_3": size_at_best_levels(bid_levels, 3),
        "yes_size_5": size_at_best_levels(bid_levels, 5),
        "no_size_1": size_at_best_levels(no_levels, 1),
        "no_size_2": size_at_best_levels(no_levels, 2),
        "no_size_3": size_at_best_levels(no_levels, 3),
        "no_size_5": size_at_best_levels(no_levels, 5),
        "imbalance_top": top_of_book_imbalance(yes_top_bid_size, yes_top_ask_size),
        "dollar_yes_bids_5": dollar_size(bid_levels, 5),
        "dollar_no_bids_5": dollar_size(no_levels, 5),
    }
    for cents in (1, 2, 3, 5):
        metrics[f"depth_within_{cents}c"] = depth_within_cents(mid, bid_levels, ask_levels, cents)
        metrics[f"dollar_depth_within_{cents}c"] = dollar_depth_within_cents(
            mid, bid_levels, ask_levels, cents
        )
    return metrics
