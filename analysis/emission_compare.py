"""Logger-vs-candle comparison with configurable bucket conventions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analysis.emission_bucketing import (
    IntervalAnchor,
    TimezoneMode,
    bucket_end_period_ts,
)

PRICE_TOLERANCE = 0.005

__all__ = [
    "PRICE_TOLERANCE",
    "book_at_or_before",
    "compare_ticker_convention",
    "prices_match",
]


def book_at_or_before(
    rows: list[tuple[datetime, float | None, float | None]], boundary: datetime
) -> tuple[float | None, float | None]:
    eligible = [row for row in rows if row[0] <= boundary]
    if not eligible:
        return None, None
    _, bid, ask = eligible[-1]
    return bid, ask


def prices_match(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= PRICE_TOLERANCE


def compare_ticker_convention(
    *,
    ticker: str,
    logger_rows: list[tuple[datetime, float | None, float | None]],
    candles: list[dict[str, Any]],
    boundaries: list[int],
    interval_anchor: IntervalAnchor,
    timezone_mode: TimezoneMode,
    bucket_offset: int,
) -> dict[str, Any]:
    """Compare logger TOB to candle closes; count silent changes per convention."""
    candle_by_ts = {int(c["end_period_ts"]): c for c in candles}
    candle_ts_set = set(candle_by_ts)
    mismatches: list[dict[str, Any]] = []
    silent_changes: list[dict[str, Any]] = []
    by_day: dict[str, dict[str, int]] = {}
    compared = 0
    matched = 0

    previous: tuple[float | None, float | None] | None = None
    for captured, bid, ask in logger_rows:
        current = (bid, ask)
        if previous is not None and current != previous:
            boundary = bucket_end_period_ts(
                captured,
                interval_anchor=interval_anchor,
                timezone_mode=timezone_mode,
                bucket_offset=bucket_offset,
            )
            if boundary not in candle_ts_set:
                silent_changes.append(
                    {
                        "ticker": ticker,
                        "ts_utc": captured.isoformat(),
                        "boundary": boundary,
                        "bid": bid,
                        "ask": ask,
                    }
                )
        previous = current

    for boundary in boundaries:
        candle = candle_by_ts.get(boundary)
        if candle is None:
            continue
        boundary_dt = datetime.fromtimestamp(boundary, tz=timezone.utc)
        logger_bid, logger_ask = book_at_or_before(logger_rows, boundary_dt)
        if logger_bid is None and logger_ask is None:
            continue
        compared += 1
        day = boundary_dt.date().isoformat()
        day_stats = by_day.setdefault(day, {"compared": 0, "matched": 0})
        day_stats["compared"] += 1
        bid_ok = prices_match(logger_bid, candle.get("bid_close"))
        ask_ok = prices_match(logger_ask, candle.get("ask_close"))
        if bid_ok and ask_ok:
            matched += 1
            day_stats["matched"] += 1
        else:
            mismatches.append(
                {
                    "ticker": ticker,
                    "end_period_ts": boundary,
                    "logger_bid": logger_bid,
                    "logger_ask": logger_ask,
                    "candle_bid": candle.get("bid_close"),
                    "candle_ask": candle.get("ask_close"),
                }
            )

    return {
        "ticker": ticker,
        "compared": compared,
        "matched": matched,
        "by_day": by_day,
        "mismatches": mismatches,
        "silent_changes": silent_changes,
    }
