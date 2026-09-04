"""Ledger prereg and held-out lock."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from wxmm.backtest.ledger import (
    BacktestResult,
    HeldOutUnlock,
    Ledger,
    config_hash,
    refuse_if_held_out_locked,
    refuse_unless_preregistered,
)
from wxmm.core.errors import ConfigNotPreregistered, CoverageReportMissing, HeldOutLockedError
from wxmm.core.money import Money
from wxmm.data.quality import CoverageReport

PREREG = Path(__file__).resolve().parents[3] / "prereg"
UTC = timezone.utc


def test_unknown_config_hash_is_refused() -> None:
    with pytest.raises(ConfigNotPreregistered):
        refuse_unless_preregistered({"prereg_id": "nope", "seed": 99}, PREREG)


def test_preregistered_smoke_config_is_accepted() -> None:
    payload = yaml.safe_load((PREREG / "stage_b1_smoke.yaml").read_text())
    registered = refuse_unless_preregistered(payload, PREREG)
    assert registered["prereg_id"] == "stage_b1_smoke"
    assert config_hash(payload) == config_hash(registered)


def test_held_out_locked_without_unlock() -> None:
    payload = yaml.safe_load((PREREG / "stage_b1_heldout.yaml").read_text())
    ledger = Ledger()
    with pytest.raises(HeldOutLockedError):
        refuse_if_held_out_locked(
            payload,
            payload,
            climate_days=(date(2026, 7, 4),),
            unlock=None,
            ledger=ledger,
        )


def test_held_out_unlock_is_loud_and_permanent() -> None:
    payload = yaml.safe_load((PREREG / "stage_b1_heldout.yaml").read_text())
    ledger = Ledger()
    refuse_if_held_out_locked(
        payload,
        payload,
        climate_days=(date(2026, 7, 4),),
        unlock=HeldOutUnlock(
            who="eugenio",
            why="session-b1-canary",
            as_of=datetime(2026, 7, 4, tzinfo=UTC),
        ),
        ledger=ledger,
    )
    assert ledger.entries[0].kind == "HELD_OUT_UNLOCK"
    assert ledger.entries[0].payload["who"] == "eugenio"
    assert ledger.entries[0].payload["loud"] is True


def test_result_without_coverage_cannot_canonicalize() -> None:
    result = BacktestResult(
        config_hash="abc",
        prereg_id="x",
        git_sha="dead",
        snapshot_id="snap",
        seed=0,
        universe=("KXHIGHNY",),
        fills=(),
        pnl=Money.zero(),
        coverage=None,
    )
    with pytest.raises(CoverageReportMissing):
        result.canonical_bytes()
    ok = BacktestResult(
        config_hash="abc",
        prereg_id="x",
        git_sha="dead",
        snapshot_id="snap",
        seed=0,
        universe=("KXHIGHNY",),
        fills=(),
        pnl=Money.zero(),
        coverage=CoverageReport(rows=()),
    )
    assert ok.canonical_bytes() == ok.canonical_bytes()
