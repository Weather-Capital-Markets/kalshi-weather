"""Kalshi bracket strike parsing and the YES predicate.

Strike structure is read from ``floor_strike`` / ``cap_strike`` /
``strike_type`` metadata, falling back to the subtitle text only when the
metadata is absent. Bracket width is not assumed.

Allowed to assume
    ``between`` brackets are inclusive at both ends, ``greater`` is
    ``high >= floor``, ``less`` is ``high <= cap``.

Must never
    Assume 2°F bins. Guess a bound that neither metadata nor subtitle states.
    Resolve a bracket from a missing high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

Role = Literal["between", "greater", "less"]

BETWEEN_SUBTITLE = re.compile(r"(\d+)\s*(?:°|deg)?\s*to\s*(\d+)\s*(?:°|deg)?", re.IGNORECASE)
TAIL_ABOVE = re.compile(r"(\d+)\s*(?:°|deg)?\s*or\s*(?:above|higher)", re.IGNORECASE)
TAIL_BELOW = re.compile(r"(\d+)\s*(?:°|deg)?\s*or\s*(?:below|lower)", re.IGNORECASE)
SUBTITLE_KEYS = ("yes_sub_title", "subtitle", "title")


@dataclass(frozen=True)
class ParsedStrike:
    role: str  # between | greater | less
    floor_f: int | None
    cap_f: int | None
    width_f: int | None
    strike_source: str


def _int_strike(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def parse_strike_from_subtitle(text: str) -> ParsedStrike | None:
    match = BETWEEN_SUBTITLE.search(text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return ParsedStrike(
            role="between",
            floor_f=low,
            cap_f=high,
            width_f=high - low + 1,
            strike_source="subtitle",
        )
    match = TAIL_ABOVE.search(text)
    if match:
        return ParsedStrike(
            role="greater",
            floor_f=int(match.group(1)),
            cap_f=None,
            width_f=None,
            strike_source="subtitle",
        )
    match = TAIL_BELOW.search(text)
    if match:
        return ParsedStrike(
            role="less",
            floor_f=None,
            cap_f=int(match.group(1)),
            width_f=None,
            strike_source="subtitle",
        )
    return None


def parse_market_strike(market: Mapping[str, Any]) -> ParsedStrike | None:
    strike_type = str(market.get("strike_type") or "").strip().lower()
    floor_f = _int_strike(market.get("floor_strike"))
    cap_f = _int_strike(market.get("cap_strike"))

    if strike_type == "between" and floor_f is not None and cap_f is not None:
        return ParsedStrike(
            role="between",
            floor_f=floor_f,
            cap_f=cap_f,
            width_f=cap_f - floor_f + 1,
            strike_source="metadata",
        )
    if strike_type == "greater" and floor_f is not None:
        return ParsedStrike(
            role="greater",
            floor_f=floor_f,
            cap_f=None,
            width_f=None,
            strike_source="metadata",
        )
    if strike_type == "less" and cap_f is not None:
        return ParsedStrike(
            role="less",
            floor_f=None,
            cap_f=cap_f,
            width_f=None,
            strike_source="metadata",
        )
    return None


def strike_of(market: Mapping[str, Any]) -> ParsedStrike | None:
    """Metadata first, subtitle text only as a fallback."""
    parsed = parse_market_strike(market)
    if parsed is not None:
        return parsed
    for key in SUBTITLE_KEYS:
        text = market.get(key)
        if isinstance(text, str) and text.strip():
            from_text = parse_strike_from_subtitle(text.strip())
            if from_text is not None:
                return from_text
    return None


def yes_won(strike: ParsedStrike, high_f: int) -> bool:
    """Bracket predicate. Raises rather than guessing a missing bound."""
    if strike.role == "between":
        if strike.floor_f is None or strike.cap_f is None:
            raise ValueError("between strike is missing a bound")
        return strike.floor_f <= high_f <= strike.cap_f
    if strike.role == "greater":
        if strike.floor_f is None:
            raise ValueError("greater strike is missing its floor")
        return high_f >= strike.floor_f
    if strike.role == "less":
        if strike.cap_f is None:
            raise ValueError("less strike is missing its cap")
        return high_f <= strike.cap_f
    raise ValueError(f"unknown strike role {strike.role!r}")
