"""Kalshi bracket strike parsing and the YES predicate.

Strike structure is read from ``floor_strike`` / ``cap_strike`` /
``strike_type`` metadata, falling back to the subtitle text only when the
metadata is absent. Bracket width is not assumed.

**The two sources disagree by one degree on the tails, and the disagreement is
silent.** Kalshi's tail metadata is strict: ``strike_type="greater"`` with
``floor_strike=97`` titles as "high >97" and subtitles as "98° or above". The
subtitle wording is inclusive of a different number. Reading ``floor_strike``
as an inclusive bound mislabels every market whose realised high lands exactly
on the strike — a boundary-only error, which is precisely where the bracket
carries information.

So ``ParsedStrike`` carries the raw parsed bounds for structural analysis and
a separate pair of *inclusive* YES bounds normalised across both sources.
``yes_won`` reads only the inclusive pair.

The strict-tail convention was checked on all 2,722 KXHIGHNY tail markets in
the archive: zero subtitle/metadata violations.

Allowed to assume
    ``between`` brackets are inclusive at both ends. Tail metadata is strict.
    Tail subtitles ("N or above" / "N or below") are inclusive.

Must never
    Assume 2°F bins. Read ``floor_strike`` / ``cap_strike`` as inclusive.
    Guess a bound that neither metadata nor subtitle states. Resolve a bracket
    from a missing high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

Role = Literal["between", "greater", "less"]

# The legacy HIGHNY era writes decimal subtitles ("44.0° or higher"). A bare
# ``(\d+)`` matches the fractional digit and silently parses a threshold of 0,
# which parses "successfully" and labels the whole tail wrong. The number must
# be a complete numeric token.
_DEG = r"(?<![\d.])(\d+(?:\.\d+)?)"
BETWEEN_SUBTITLE = re.compile(
    rf"{_DEG}\s*(?:°|deg)?\s*to\s*{_DEG}\s*(?:°|deg)?", re.IGNORECASE
)
TAIL_ABOVE = re.compile(rf"{_DEG}\s*(?:°|deg)?\s*or\s*(?:above|higher)", re.IGNORECASE)
TAIL_BELOW = re.compile(rf"{_DEG}\s*(?:°|deg)?\s*or\s*(?:below|lower)", re.IGNORECASE)
SUBTITLE_KEYS = ("yes_sub_title", "subtitle", "title")


@dataclass(frozen=True)
class ParsedStrike:
    """``floor_f``/``cap_f`` are as parsed. ``yes_*`` are normalised inclusive."""

    role: str  # between | greater | less
    floor_f: int | None
    cap_f: int | None
    width_f: int | None
    strike_source: str
    yes_floor_f: int | None = None  # smallest high_f resolving YES; None = unbounded
    yes_cap_f: int | None = None  # largest high_f resolving YES; None = unbounded


def _int_strike(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _deg(text: str) -> int:
    """Subtitle degrees are whole even when written "44.0"."""
    value = float(text)
    if value != int(value):
        raise ValueError(f"non-integer bracket bound {text!r}")
    return int(value)


def parse_strike_from_subtitle(text: str) -> ParsedStrike | None:
    """Subtitle wording is inclusive: "98° or above" means ``high >= 98``."""
    match = BETWEEN_SUBTITLE.search(text)
    if match:
        low, high = _deg(match.group(1)), _deg(match.group(2))
        return ParsedStrike(
            role="between",
            floor_f=low,
            cap_f=high,
            width_f=high - low + 1,
            strike_source="subtitle",
            yes_floor_f=low,
            yes_cap_f=high,
        )
    match = TAIL_ABOVE.search(text)
    if match:
        threshold = _deg(match.group(1))
        return ParsedStrike(
            role="greater",
            floor_f=threshold,
            cap_f=None,
            width_f=None,
            strike_source="subtitle",
            yes_floor_f=threshold,
            yes_cap_f=None,
        )
    match = TAIL_BELOW.search(text)
    if match:
        threshold = _deg(match.group(1))
        return ParsedStrike(
            role="less",
            floor_f=None,
            cap_f=threshold,
            width_f=None,
            strike_source="subtitle",
            yes_floor_f=None,
            yes_cap_f=threshold,
        )
    return None


def parse_market_strike(market: Mapping[str, Any]) -> ParsedStrike | None:
    """Tail metadata is strict: ``floor_strike=97`` means ``high > 97``."""
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
            yes_floor_f=floor_f,
            yes_cap_f=cap_f,
        )
    if strike_type == "greater" and floor_f is not None:
        return ParsedStrike(
            role="greater",
            floor_f=floor_f,
            cap_f=None,
            width_f=None,
            strike_source="metadata",
            yes_floor_f=floor_f + 1,
            yes_cap_f=None,
        )
    if strike_type == "less" and cap_f is not None:
        return ParsedStrike(
            role="less",
            floor_f=None,
            cap_f=cap_f,
            width_f=None,
            strike_source="metadata",
            yes_floor_f=None,
            yes_cap_f=cap_f - 1,
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
    """Bracket predicate over the normalised inclusive bounds.

    Reads ``yes_floor_f`` / ``yes_cap_f`` only. Raises rather than guessing a
    missing bound, because a guess here is wrong exactly at the boundary.
    """
    if strike.yes_floor_f is None and strike.yes_cap_f is None:
        raise ValueError(f"{strike.role} strike has no inclusive bound")
    if strike.role == "between" and (
        strike.yes_floor_f is None or strike.yes_cap_f is None
    ):
        raise ValueError("between strike is missing a bound")
    if strike.yes_floor_f is not None and high_f < strike.yes_floor_f:
        return False
    if strike.yes_cap_f is not None and high_f > strike.yes_cap_f:
        return False
    return True
