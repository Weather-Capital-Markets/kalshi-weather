"""Stage 0 decision bands — locked before the VPS sweep."""

from __future__ import annotations

from analysis.emission_decision import decide_from_table


def _row(key: str, match_rate: float, n: int = 1000) -> dict[str, object]:
    return {
        "key": key,
        "match_rate": match_rate,
        "boundaries_compared": n,
        "matched": int(round(match_rate * n)),
        "silent_count": 10,
        "row_status": "computed",
        "provenance": "computed",
    }


def _twelve(rates: list[float]) -> list[dict[str, object]]:
    keys: list[str] = []
    for anchor in ("interval_start", "interval_end"):
        for tz in ("UTC", "ET"):
            for off in (-1, 0, 1):
                keys.append(f"{anchor}|{tz}|{off:+d}")
    assert len(keys) == 12
    assert len(rates) == 12
    return [_row(k, r) for k, r in zip(keys, rates, strict=True)]


def test_join_bug_at_or_above_60() -> None:
    rates = [0.01] * 11 + [0.72]
    decision = decide_from_table(_twelve(rates))
    assert decision.kind == "JOIN_BUG"
    assert decision.branch == "full_corpus"
    assert decision.reconstruction_error_bound == "1-match_rate=0.280000@interval_end|ET|+1"


def test_partial_band_does_not_branch() -> None:
    rates = [0.01] * 11 + [0.35]
    decision = decide_from_table(_twelve(rates))
    assert decision.kind == "PARTIAL"
    assert decision.branch is None
    assert decision.reconstruction_error_bound is None
    assert "adjudication" in decision.note.lower()


def test_low_band_requires_wellposedness_not_falsification() -> None:
    rates = [0.01] * 12
    decision = decide_from_table(_twelve(rates))
    assert decision.kind == "LOW"
    assert decision.branch is None
    assert "wellposed" in decision.note.lower() or "ILL_POSED" in decision.note
    assert "falsified" in decision.note.lower()


def test_incomplete_table_has_no_branch() -> None:
    decision = decide_from_table([_row("interval_start|UTC|+0", 0.9)])
    assert decision.kind == "INCOMPLETE"
    assert decision.branch is None


def test_transcribed_rows_do_not_count() -> None:
    rows = _twelve([0.8] + [0.01] * 11)
    rows[0]["provenance"] = "transcribed_not_computed"
    rows[0]["boundaries_compared"] = 0
    decision = decide_from_table(rows)
    assert decision.kind == "INCOMPLETE"
