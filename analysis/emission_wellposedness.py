"""Well-posedness check before concluding emission-on-change is falsified.

Allowed to assume
    Logger orderbook JSONL and Kalshi candlesticks are both available for the
    declared Stage 0 window. Sample size is small (hand audit of ~10 changes).

Must never
    Conclude \"falsified\" from twelve low match rates alone. Treat a
    trade-derived candle series as interchangeable with quote-state books
    without checking. Invent alignment after seeing the sample.

If candles are trade-derived and the logger records quote state, no join
convention can align them. Then 0.9% is the expected result, not a defect, and
the correct finding is CROSS_CHECK_ILL_POSED (redesign required), not
emission-on-change falsified. Those have different consequences for the
historical corpus: falsified means books are degraded; ill-posed means they
are merely unvalidated by this test.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from analysis.emission_decision import FinalizedStage0, finalize_stage0

Finding = Literal[
    "WELL_POSED_SAME_OBJECT",
    "CROSS_CHECK_ILL_POSED",
    "INCONCLUSIVE",
    "NOT_RUN",
]

CORPUS_CONSEQUENCE = {
    "WELL_POSED_SAME_OBJECT": (
        "emission_on_change_falsified — historical reconstructed books degraded; "
        "activate forward_logger_only"
    ),
    "CROSS_CHECK_ILL_POSED": (
        "cross_check_ill_posed_redesign_required — historical books unvalidated "
        "by this test, not proven degraded; redesign the cross-check"
    ),
}


@dataclass(frozen=True, slots=True)
class ChangeSample:
    ticker: str
    ts_utc: str
    logger_bid: float | None
    logger_ask: float | None
    candle_present: bool | None
    candle_bid: float | None
    candle_ask: float | None
    could_represent: bool | None
    note: str


@dataclass(frozen=True, slots=True)
class WellposednessReport:
    finding: Finding
    samples: list[ChangeSample]
    declared_window_start: str
    declared_window_end: str
    note: str
    corpus_consequence: str | None = None


def empty_report(*, start: str, end: str) -> WellposednessReport:
    return WellposednessReport(
        finding="NOT_RUN",
        samples=[],
        declared_window_start=start,
        declared_window_end=end,
        corpus_consequence=None,
        note=(
            "Hand audit not yet filled. Pick 10 logger book changes with timestamps "
            "in the declared window and check whether the corresponding candle could "
            "in principle have represented that quote-state change (same object, same "
            "granularity). Record could_represent per row, then set finding to "
            "WELL_POSED_SAME_OBJECT or CROSS_CHECK_ILL_POSED."
        ),
    )


def adjudicate(samples: list[ChangeSample]) -> Finding:
    if len(samples) < 10:
        return "INCONCLUSIVE"
    answered = [s for s in samples if s.could_represent is not None]
    if len(answered) < 10:
        return "INCONCLUSIVE"
    yes = sum(1 for s in answered if s.could_represent)
    no = sum(1 for s in answered if s.could_represent is False)
    if yes >= 8:
        return "WELL_POSED_SAME_OBJECT"
    if no >= 8:
        return "CROSS_CHECK_ILL_POSED"
    return "INCONCLUSIVE"


def with_finding(report: WellposednessReport, finding: Finding) -> WellposednessReport:
    return WellposednessReport(
        finding=finding,
        samples=report.samples,
        declared_window_start=report.declared_window_start,
        declared_window_end=report.declared_window_end,
        note=report.note,
        corpus_consequence=CORPUS_CONSEQUENCE.get(finding),
    )


def write_report(report: WellposednessReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    finding = report.finding
    if report.samples and finding == "NOT_RUN":
        finding = adjudicate(report.samples)
    consequence = report.corpus_consequence or CORPUS_CONSEQUENCE.get(finding)
    payload = {
        "finding": finding,
        "declared_window_start": report.declared_window_start,
        "declared_window_end": report.declared_window_end,
        "note": report.note,
        "corpus_consequence": consequence,
        "samples": [asdict(s) for s in report.samples],
        "written_at": datetime.now().isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def apply_finding_to_sweep(
    sweep_path: Path,
    *,
    finding: Finding,
) -> FinalizedStage0:
    """Patch Stage 0 sweep artifact after the hand well-posedness audit."""
    raw = json.loads(sweep_path.read_text(encoding="utf-8"))
    decision_raw = raw.get("decision") or {}
    if decision_raw.get("kind") != "LOW":
        raise ValueError(
            f"well-posedness applies only when decision.kind=LOW; got {decision_raw.get('kind')}"
        )
    from analysis.emission_decision import SweepDecision

    decision = SweepDecision(
        kind="LOW",
        max_match_rate=float(decision_raw.get("max_match_rate") or 0.0),
        winning_key=decision_raw.get("winning_key"),
        branch=None,
        reconstruction_error_bound=None,
        note=str(decision_raw.get("note") or ""),
    )
    finalized = finalize_stage0(decision, wellposedness_finding=finding)
    raw["finalized"] = {
        "decision_kind": finalized.decision_kind,
        "wellposedness_finding": finalized.wellposedness_finding,
        "branch": finalized.branch,
        "reconstruction_error_bound": finalized.reconstruction_error_bound,
        "corpus_consequence": finalized.corpus_consequence,
        "note": finalized.note,
    }
    if finalized.branch is not None:
        raw["branch"] = finalized.branch
    elif "branch" in raw:
        del raw["branch"]
    sweep_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    return finalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scaffold well-posedness audit before falsifying emit-on-change"
    )
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("analysis/out/emission_wellposedness.json"),
    )
    parser.add_argument(
        "--apply-finding",
        type=str,
        choices=[
            "WELL_POSED_SAME_OBJECT",
            "CROSS_CHECK_ILL_POSED",
            "INCONCLUSIVE",
            "NOT_RUN",
        ],
        default=None,
        help="After hand audit, patch the Stage 0 sweep artifact with this finding",
    )
    parser.add_argument(
        "--sweep-artifact",
        type=Path,
        default=Path("analysis/out/emission_convention_sweep.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = empty_report(start=args.start, end=args.end)
    path = write_report(report, args.out)
    print(f"wrote scaffold {path} finding={report.finding}")
    print(report.note)
    if args.apply_finding is not None:
        finalized = apply_finding_to_sweep(
            args.sweep_artifact, finding=args.apply_finding
        )
        print(
            f"patched {args.sweep_artifact}: "
            f"corpus_consequence={finalized.corpus_consequence} "
            f"branch={finalized.branch}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
