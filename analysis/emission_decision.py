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
