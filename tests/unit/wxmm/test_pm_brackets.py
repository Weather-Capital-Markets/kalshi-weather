"""Polymarket NYC ladder is 11 exhaustive disjoint bins; Kalshi widths not coded."""

from __future__ import annotations

from wxmm.venues.polymarket.brackets import assert_eleven_exhaustive, polymarket_nyc_brackets


def test_eleven_brackets_cover_whole_degrees() -> None:
    assert_eleven_exhaustive()
    brackets = polymarket_nyc_brackets()
    assert brackets[0].kind == "tail_below"
    assert brackets[-1].kind == "tail_above"
    hits = {t: sum(1 for b in brackets if b.contains(t)) for t in range(70, 100)}
    assert all(count == 1 for count in hits.values())
