"""Stage 0 decision rule for the 12-way emission convention sweep.

Preregistered BEFORE any VPS sweep run. Decision is on the maximum match rate
across the twelve conventions. Intermediate rates are the most likely bad
outcome and must not be branched after seeing the number.

Bands (max over the 12 rows):
  - max >= 0.60           -> JOIN_BUG. Adopt that convention.
                            reconstruction_error_bound = 1 - match_rate.
                            branch = full_corpus.
  - 0.10 <= max < 0.60    -> PARTIAL. Do not branch. Do not average.
                            Split the winning convention by market and by day;
                            written adjudication required before any branch.
  - max < 0.10            -> LOW. Do NOT conclude \"falsified\" yet.
                            Run the well-posedness check first
                            (analysis/emission_wellposedness.py).

Well-posedness (when LOW):
  Confirm logger books and candles describe the same object at the same
  granularity. If they do not, the finding is CROSS_CHECK_ILL_POSED, not
  emission-on-change falsified. Those have different consequences for the
  historical corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DecisionKind = Literal["JOIN_BUG", "PARTIAL", "LOW", "INCOMPLETE"]
WellposednessFinding = Literal[
    "WELL_POSED_SAME_OBJECT",
    "CROSS_CHECK_ILL_POSED",
    "INCONCLUSIVE",
    "NOT_RUN",
]
CorpusConsequence = Literal[
    "full_corpus_usable",
    "partial_pending_adjudication",
    "emission_on_change_falsified",
    "cross_check_ill_posed_redesign_required",
    "awaiting_wellposedness",
    "incomplete",
]

JOIN_BUG_MIN = 0.60
PARTIAL_MIN = 0.10


@dataclass(frozen=True, slots=True)
class SweepDecision:
    kind: DecisionKind
    max_match_rate: float
    winning_key: str | None
    branch: str | None
    reconstruction_error_bound: str | None
    note: str


@dataclass(frozen=True, slots=True)
class FinalizedStage0:
    """Stage 0 outcome after optional well-posedness (required when kind=LOW)."""

    decision_kind: DecisionKind
    wellposedness_finding: WellposednessFinding | None
    branch: str | None
    reconstruction_error_bound: str | None
    corpus_consequence: CorpusConsequence
    note: str


def decide_from_table(table: list[dict[str, Any]]) -> SweepDecision:
    """Decide from computed rows only. Transcribed / null rows are ignored."""
    computed = [
        row
        for row in table
        if isinstance(row.get("boundaries_compared"), int)
        and int(row["boundaries_compared"]) > 0
        and row.get("provenance") != "transcribed_not_computed"
        and row.get("row_status") not in {"not_computed", "transcribed_not_computed"}
        and row.get("match_rate") is not None
    ]
    if len(computed) < 12:
        return SweepDecision(
            kind="INCOMPLETE",
            max_match_rate=0.0,
            winning_key=None,
            branch=None,
            reconstruction_error_bound=None,
            note=f"only {len(computed)}/12 computed rows; branch absent",
        )

    winner = max(computed, key=lambda row: float(row["match_rate"]))
    max_rate = float(winner["match_rate"])
    key = str(winner["key"])

    if max_rate >= JOIN_BUG_MIN:
        bound = 1.0 - max_rate
        return SweepDecision(
            kind="JOIN_BUG",
            max_match_rate=max_rate,
            winning_key=key,
            branch="full_corpus",
            reconstruction_error_bound=f"1-match_rate={bound:.6f}@{key}",
            note="join bug located; adopt winning convention; full historical corpus usable",
        )

    if max_rate >= PARTIAL_MIN:
        return SweepDecision(
            kind="PARTIAL",
            max_match_rate=max_rate,
            winning_key=key,
            branch=None,
            reconstruction_error_bound=None,
            note=(
                "PARTIAL alignment (0.10 <= max < 0.60). Do not branch or average. "
                "Split winning convention by market and by day; written adjudication "
                "required before any branch."
            ),
        )

    return SweepDecision(
        kind="LOW",
        max_match_rate=max_rate,
        winning_key=key,
        branch=None,
        reconstruction_error_bound=None,
        note=(
            "max match rate < 0.10. Do not conclude emission-on-change falsified yet. "
            "Run analysis/emission_wellposedness.py first: if logger books and candles "
            "do not describe the same object, finding is CROSS_CHECK_ILL_POSED "
            "(historical corpus unvalidated by this test), not falsified (degraded books)."
        ),
    )


def finalize_stage0(
    decision: SweepDecision,
    *,
    wellposedness_finding: WellposednessFinding | None = None,
) -> FinalizedStage0:
    """Lock corpus consequence. LOW requires a well-posedness finding first."""
    if decision.kind == "INCOMPLETE":
        return FinalizedStage0(
            decision_kind=decision.kind,
            wellposedness_finding=None,
            branch=None,
            reconstruction_error_bound=None,
            corpus_consequence="incomplete",
            note=decision.note,
        )
    if decision.kind == "JOIN_BUG":
        return FinalizedStage0(
            decision_kind=decision.kind,
            wellposedness_finding=None,
            branch=decision.branch,
            reconstruction_error_bound=decision.reconstruction_error_bound,
            corpus_consequence="full_corpus_usable",
            note=decision.note,
        )
    if decision.kind == "PARTIAL":
        return FinalizedStage0(
            decision_kind=decision.kind,
            wellposedness_finding=None,
            branch=None,
            reconstruction_error_bound=None,
            corpus_consequence="partial_pending_adjudication",
            note=(
                f"{decision.note} Artifact must include winner_match_distribution "
                "(by_market and by_day match rates) for written adjudication."
            ),
        )

    # LOW
    finding = wellposedness_finding or "NOT_RUN"
    if finding in {None, "NOT_RUN", "INCONCLUSIVE"}:
        return FinalizedStage0(
            decision_kind="LOW",
            wellposedness_finding=finding,
            branch=None,
            reconstruction_error_bound=None,
            corpus_consequence="awaiting_wellposedness",
            note=(
                "LOW match rates alone do not falsify emit-on-change. "
                "Complete the hand audit of ~10 logger book changes vs candles "
                "(analysis/emission_wellposedness.py) and record WELL_POSED_SAME_OBJECT "
                "or CROSS_CHECK_ILL_POSED."
            ),
        )
    if finding == "WELL_POSED_SAME_OBJECT":
        return FinalizedStage0(
            decision_kind="LOW",
            wellposedness_finding=finding,
            branch="forward_logger_only",
            reconstruction_error_bound=None,
            corpus_consequence="emission_on_change_falsified",
            note=(
                "Cross-check is well posed (same object / granularity) and max match "
                "< 0.10 → emission-on-change falsified for historical books. "
                "Activate forward_logger_only; historical reconstructed books are degraded."
            ),
        )
    # CROSS_CHECK_ILL_POSED
    return FinalizedStage0(
        decision_kind="LOW",
        wellposedness_finding=finding,
        branch=None,
        reconstruction_error_bound=None,
        corpus_consequence="cross_check_ill_posed_redesign_required",
        note=(
            "Logger books and candles do not describe the same object at the same "
            "granularity. No join convention can align them; low match is expected, "
            "not a defect. Historical corpus is unvalidated by this test (needs a "
            "different cross-check), not proven degraded."
        ),
    )


def winner_match_distribution(
    *,
    winning_key: str,
    per_ticker: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Split one convention's match rate by market and by day (PARTIAL evidence)."""
    by_market: dict[str, dict[str, Any]] = {}
    by_day_agg: dict[str, dict[str, int]] = {}
    for ticker, result in sorted(per_ticker.items()):
        compared = int(result.get("compared", 0))
        matched = int(result.get("matched", 0))
        by_market[ticker] = {
            "compared": compared,
            "matched": matched,
            "match_rate": (matched / compared) if compared else 0.0,
        }
        for day, stats in (result.get("by_day") or {}).items():
            bucket = by_day_agg.setdefault(day, {"compared": 0, "matched": 0})
            bucket["compared"] += int(stats.get("compared", 0))
            bucket["matched"] += int(stats.get("matched", 0))
    by_day = {
        day: {
            "compared": stats["compared"],
            "matched": stats["matched"],
            "match_rate": (
                (stats["matched"] / stats["compared"]) if stats["compared"] else 0.0
            ),
        }
        for day, stats in sorted(by_day_agg.items())
    }
    rates = [row["match_rate"] for row in by_market.values() if row["compared"] > 0]
    day_rates = [row["match_rate"] for row in by_day.values() if row["compared"] > 0]
    return {
        "winning_key": winning_key,
        "by_market": by_market,
        "by_day": by_day,
        "market_match_rate_min": min(rates) if rates else None,
        "market_match_rate_max": max(rates) if rates else None,
        "day_match_rate_min": min(day_rates) if day_rates else None,
        "day_match_rate_max": max(day_rates) if day_rates else None,
        "note": (
            "Distribution required for PARTIAL (mixture / mid-period convention change). "
            "Do not average into a single bound; written adjudication before any branch."
        ),
    }
