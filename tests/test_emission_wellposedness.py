"""Well-posedness adjudication before emit-on-change falsification."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.emission_wellposedness import (
    ChangeSample,
    adjudicate,
    apply_finding_to_sweep,
    empty_report,
    write_report,
)


def _sample(could: bool | None) -> ChangeSample:
    return ChangeSample(
        ticker="KXHIGHNY-26AUG19-T90",
        ts_utc="2026-08-19T14:00:00+00:00",
        logger_bid=0.4,
        logger_ask=0.45,
        candle_present=True,
        candle_bid=0.4,
        candle_ask=0.45,
        could_represent=could,
        note="",
    )


def test_adjudicate_needs_ten_answered() -> None:
    assert adjudicate([_sample(True)] * 9) == "INCONCLUSIVE"
    assert adjudicate([_sample(True)] * 10) == "WELL_POSED_SAME_OBJECT"
    assert adjudicate([_sample(False)] * 10) == "CROSS_CHECK_ILL_POSED"
    assert adjudicate([_sample(True)] * 5 + [_sample(False)] * 5) == "INCONCLUSIVE"


def test_apply_finding_patches_low_sweep(tmp_path: Path) -> None:
    sweep = tmp_path / "emission_convention_sweep.json"
    sweep.write_text(
        json.dumps(
            {
                "sweep_status": "COMPLETE",
                "decision": {
                    "kind": "LOW",
                    "max_match_rate": 0.02,
                    "winning_key": "interval_start|UTC|+0",
                    "note": "low",
                },
                "table": [],
            }
        ),
        encoding="utf-8",
    )
    finalized = apply_finding_to_sweep(sweep, finding="CROSS_CHECK_ILL_POSED")
    assert finalized.corpus_consequence == "cross_check_ill_posed_redesign_required"
    assert finalized.branch is None
    raw = json.loads(sweep.read_text(encoding="utf-8"))
    assert raw["finalized"]["corpus_consequence"] == "cross_check_ill_posed_redesign_required"
    assert "branch" not in raw

    finalized2 = apply_finding_to_sweep(sweep, finding="WELL_POSED_SAME_OBJECT")
    assert finalized2.branch == "forward_logger_only"
    raw2 = json.loads(sweep.read_text(encoding="utf-8"))
    assert raw2["branch"] == "forward_logger_only"
    assert raw2["finalized"]["corpus_consequence"] == "emission_on_change_falsified"


def test_scaffold_write(tmp_path: Path) -> None:
    path = write_report(
        empty_report(start="2026-08-19", end="2026-09-12"),
        tmp_path / "wellposed.json",
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["finding"] == "NOT_RUN"
    assert raw["corpus_consequence"] is None
