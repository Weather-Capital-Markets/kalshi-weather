"""Derive top-of-book yes prices from Kalshi orderbook payloads."""

from __future__ import annotations

from typing import Any


def _best_yes_bid(orderbook_fp: dict[str, Any]) -> float | None:
    levels = orderbook_fp.get("yes_dollars")
    if not isinstance(levels, list) or not levels:
        return None
    prices: list[float] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or not level:
            continue
        try:
            prices.append(float(level[0]))
        except (TypeError, ValueError):
            continue
    return max(prices) if prices else None


def _best_yes_ask(orderbook_fp: dict[str, Any]) -> float | None:
    levels = orderbook_fp.get("no_dollars")
    if not isinstance(levels, list) or not levels:
        return None
    prices: list[float] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or not level:
            continue
        try:
            prices.append(float(level[0]))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    return 1.0 - max(prices)


def top_of_book_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (yes_bid, yes_ask) from a logger orderbook API payload."""
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None, None
    return _best_yes_bid(book), _best_yes_ask(book)
