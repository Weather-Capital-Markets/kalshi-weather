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
from typing import Any, Literal

Finding = Literal[
    "WELL_POSED_SAME_OBJECT",
    "CROSS_CHECK_ILL_POSED",
    "INCONCLUSIVE",
    "NOT_RUN",
]


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


def empty_report(*, start: str, end: str) -> WellposednessReport:
    return WellposednessReport(
        finding="NOT_RUN",
        samples=[],
        declared_window_start=start,
        declared_window_end=end,
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


def write_report(report: WellposednessReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "finding": report.finding,
        "declared_window_start": report.declared_window_start,
        "declared_window_end": report.declared_window_end,
        "note": report.note,
        "samples": [asdict(s) for s in report.samples],
        "written_at": datetime.now().isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = empty_report(start=args.start, end=args.end)
    path = write_report(report, args.out)
    print(f"wrote scaffold {path} finding={report.finding}")
    print(report.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
