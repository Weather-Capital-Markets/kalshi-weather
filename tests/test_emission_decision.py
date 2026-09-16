"""Stage 0 decision bands — locked before the VPS sweep."""

from __future__ import annotations

from analysis.emission_decision import (
    decide_from_table,
    finalize_stage0,
    winner_match_distribution,
)


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
    finalized = finalize_stage0(decision)
    assert finalized.corpus_consequence == "full_corpus_usable"
    assert finalized.branch == "full_corpus"


def test_partial_band_does_not_branch() -> None:
    rates = [0.01] * 11 + [0.35]
    decision = decide_from_table(_twelve(rates))
    assert decision.kind == "PARTIAL"
    assert decision.branch is None
    assert decision.reconstruction_error_bound is None
    assert "adjudication" in decision.note.lower()
    finalized = finalize_stage0(decision)
    assert finalized.corpus_consequence == "partial_pending_adjudication"
    assert finalized.branch is None


def test_low_band_requires_wellposedness_not_falsification() -> None:
    rates = [0.01] * 12
    decision = decide_from_table(_twelve(rates))
    assert decision.kind == "LOW"
    assert decision.branch is None
    assert "wellposed" in decision.note.lower() or "ILL_POSED" in decision.note
    assert "falsified" in decision.note.lower()
    awaiting = finalize_stage0(decision)
    assert awaiting.corpus_consequence == "awaiting_wellposedness"
    assert awaiting.branch is None


def test_low_wellposed_same_object_falsifies() -> None:
    decision = decide_from_table(_twelve([0.01] * 12))
    finalized = finalize_stage0(
        decision, wellposedness_finding="WELL_POSED_SAME_OBJECT"
    )
    assert finalized.corpus_consequence == "emission_on_change_falsified"
    assert finalized.branch == "forward_logger_only"


def test_low_ill_posed_does_not_falsify() -> None:
    decision = decide_from_table(_twelve([0.01] * 12))
    finalized = finalize_stage0(
        decision, wellposedness_finding="CROSS_CHECK_ILL_POSED"
    )
    assert finalized.corpus_consequence == "cross_check_ill_posed_redesign_required"
    assert finalized.branch is None
    assert (
        "unvalidated" in finalized.note.lower()
        or "redesign" in finalized.note.lower()
    )


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


def test_winner_match_distribution_by_market_and_day() -> None:
    dist = winner_match_distribution(
        winning_key="interval_end|ET|+0",
        per_ticker={
            "A": {
                "compared": 100,
                "matched": 40,
                "by_day": {
                    "2026-08-19": {"compared": 50, "matched": 10},
                    "2026-08-20": {"compared": 50, "matched": 30},
                },
            },
            "B": {
                "compared": 100,
                "matched": 20,
                "by_day": {
                    "2026-08-19": {"compared": 40, "matched": 5},
                    "2026-08-20": {"compared": 60, "matched": 15},
                },
            },
        },
    )
    assert dist["by_market"]["A"]["match_rate"] == 0.4
    assert dist["by_market"]["B"]["match_rate"] == 0.2
    assert dist["by_day"]["2026-08-19"]["compared"] == 90
    assert dist["by_day"]["2026-08-19"]["matched"] == 15
    assert dist["market_match_rate_min"] == 0.2
    assert dist["market_match_rate_max"] == 0.4
    assert "average" in dist["note"].lower()
