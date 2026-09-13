"""Shared candle bucket conventions for emission validation sweeps.

Twelve combinations: {interval_start, interval_end} x {UTC, ET} x {-1, 0, +1}.
Kalshi ``end_period_ts`` is the period **end** in UTC unix seconds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Sequence
from zoneinfo import ZoneInfo

IntervalAnchor = Literal["interval_start", "interval_end"]
TimezoneMode = Literal["UTC", "ET"]

ET = ZoneInfo("America/New_York")
PERIOD_SEC = 60

CONVENTIONS: tuple[tuple[IntervalAnchor, TimezoneMode, int], ...] = tuple(
    (anchor, tz_mode, offset)
    for anchor in ("interval_start", "interval_end")
    for tz_mode in ("UTC", "ET")
    for offset in (-1, 0, 1)
)


def convention_key(
    interval_anchor: IntervalAnchor,
    timezone_mode: TimezoneMode,
    bucket_offset: int,
) -> str:
    return f"{interval_anchor}|{timezone_mode}|{bucket_offset:+d}"


def parse_convention_key(key: str) -> tuple[IntervalAnchor, TimezoneMode, int]:
    anchor, tz_mode, offset_raw = key.split("|")
    return anchor, tz_mode, int(offset_raw)  # type: ignore[return-value]


def bucket_end_period_ts(
    captured: datetime,
    *,
    interval_anchor: IntervalAnchor,
    timezone_mode: TimezoneMode,
    bucket_offset: int,
) -> int:
    """Map a logger capture instant to a candle ``end_period_ts`` under a convention."""
    if captured.tzinfo is None:
        raise ValueError("captured must be timezone-aware")
    if interval_anchor not in {"interval_start", "interval_end"}:
        raise ValueError(f"unknown interval_anchor {interval_anchor!r}")
    if timezone_mode not in {"UTC", "ET"}:
        raise ValueError(f"unknown timezone_mode {timezone_mode!r}")
    if bucket_offset not in {-1, 0, 1}:
        raise ValueError(f"bucket_offset must be -1, 0, or +1, not {bucket_offset}")

    if timezone_mode == "UTC":
        local = captured.astimezone(timezone.utc)
    else:
        local = captured.astimezone(ET)

    floored = int(local.timestamp())
    floored -= floored % PERIOD_SEC
    if interval_anchor == "interval_end":
        floored += PERIOD_SEC
    floored += bucket_offset * PERIOD_SEC
    return floored


def all_convention_keys() -> list[str]:
    return [
        convention_key(anchor, tz_mode, offset)
        for anchor, tz_mode, offset in CONVENTIONS
    ]


def iter_conventions() -> Sequence[tuple[IntervalAnchor, TimezoneMode, int]]:
    return CONVENTIONS
