"""Bracket strike parsing. Both traps here were found against the real archive.

1. Tail metadata is strict and the subtitle for the same bracket is inclusive
   of a different number. Reading ``floor_strike`` as inclusive mislabels every
   market whose realised high lands exactly on the strike.
2. Legacy subtitles are decimal ("44.0° or higher"). A bare ``\\d+`` matches the
   fractional digit and parses a threshold of 0, which looks like a successful
   parse and labels the whole tail wrong.
"""

from __future__ import annotations

import pytest

from wxmm.settlement.brackets import (
    parse_market_strike,
    parse_strike_from_subtitle,
    strike_of,
    yes_won,
)


def test_greater_metadata_is_strict() -> None:
    """floor_strike=97 titles as ">97" and subtitles as "98 or above"."""
    strike = parse_market_strike(
        {"strike_type": "greater", "floor_strike": 97, "cap_strike": None}
    )
    assert strike is not None
    assert strike.floor_f == 97
    assert strike.yes_floor_f == 98
    assert yes_won(strike, 98) is True
    assert yes_won(strike, 97) is False


def test_less_metadata_is_strict() -> None:
    """cap_strike=90 titles as "<90" and subtitles as "89 or below"."""
    strike = parse_market_strike(
        {"strike_type": "less", "floor_strike": None, "cap_strike": 90}
    )
    assert strike is not None
    assert strike.cap_f == 90
    assert strike.yes_cap_f == 89
    assert yes_won(strike, 89) is True
    assert yes_won(strike, 90) is False


def test_between_metadata_is_inclusive_both_ends() -> None:
    strike = parse_market_strike(
        {"strike_type": "between", "floor_strike": 96, "cap_strike": 97}
    )
    assert strike is not None
    assert (strike.yes_floor_f, strike.yes_cap_f) == (96, 97)
    assert [yes_won(strike, h) for h in (95, 96, 97, 98)] == [False, True, True, False]


def test_metadata_and_subtitle_agree_after_normalisation() -> None:
    """The same bracket from either source must resolve identically."""
    from_meta = parse_market_strike(
        {"strike_type": "greater", "floor_strike": 97, "cap_strike": None}
    )
    from_text = parse_strike_from_subtitle("98° or above")
    assert from_meta is not None and from_text is not None
    assert from_meta.floor_f != from_text.floor_f  # raw bounds differ by one
    assert from_meta.yes_floor_f == from_text.yes_floor_f
    for high in range(90, 105):
        assert yes_won(from_meta, high) == yes_won(from_text, high)


@pytest.mark.parametrize(
    ("text", "yes_floor", "yes_cap"),
    [
        ("44.0° or higher", 44, None),
        ("35.0° or lower", None, 35),
        ("98° or above", 98, None),
        ("89° or below", None, 89),
        ("36° to 37°", 36, 37),
    ],
)
def test_decimal_subtitles_do_not_parse_as_zero(
    text: str, yes_floor: int | None, yes_cap: int | None
) -> None:
    strike = parse_strike_from_subtitle(text)
    assert strike is not None
    assert (strike.yes_floor_f, strike.yes_cap_f) == (yes_floor, yes_cap)


def test_legacy_decimal_tail_resolves_correctly() -> None:
    """HIGHNY-22DEC21-T43 is "44.0° or higher"; the day's high was 40."""
    strike = parse_strike_from_subtitle("44.0° or higher")
    assert strike is not None
    assert yes_won(strike, 40) is False
    assert yes_won(strike, 44) is True


def test_untyped_market_falls_back_to_subtitle() -> None:
    market = {
        "strike_type": None,
        "floor_strike": None,
        "cap_strike": None,
        "yes_sub_title": "35° to 36°",
    }
    strike = strike_of(market)
    assert strike is not None
    assert strike.strike_source == "subtitle"
    assert yes_won(strike, 35) is True
    assert yes_won(strike, 37) is False


def test_metadata_wins_over_subtitle() -> None:
    market = {
        "strike_type": "between",
        "floor_strike": 41,
        "cap_strike": 42,
        "yes_sub_title": "41° to 42°",
    }
    strike = strike_of(market)
    assert strike is not None
    assert strike.strike_source == "metadata"


def test_unbounded_strike_refuses_rather_than_guessing() -> None:
    from wxmm.settlement.brackets import ParsedStrike

    naked = ParsedStrike(
        role="greater", floor_f=None, cap_f=None, width_f=None, strike_source="metadata"
    )
    with pytest.raises(ValueError, match="no inclusive bound"):
        yes_won(naked, 80)


def test_non_integer_bound_refuses() -> None:
    assert parse_strike_from_subtitle("no degrees here") is None
    with pytest.raises(ValueError, match="non-integer"):
        parse_strike_from_subtitle("44.5° or higher")
