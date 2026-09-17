"""Snooping guard: immutable run records, prereg, held-out lock."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field

from wxmm.backtest.fills import Fill
from wxmm.core.errors import (
    ConfigNotPreregistered,
    CoverageReportMissing,
    HeldOutLockedError,
)
from wxmm.core.money import Money
from wxmm.core.utc import require_utc
from wxmm.data.quality import CoverageReport


class HeldOutWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    locked: bool = True


class PreregConfig(BaseModel):
    """Pre-registered backtest config. ``run()`` refuses hashes not in ``prereg/``."""

    model_config = ConfigDict(extra="allow")

    prereg_id: str
    seed: int = 0
    universe: list[str] = Field(default_factory=list)
    snapshot_id: str = ""
    held_out: HeldOutWindow | None = None


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default).encode()


def _json_default(value: object) -> object:
    if isinstance(value, Money):
        return str(value.amount)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def config_hash(config: Mapping[str, Any]) -> str:
    body = {key: config[key] for key in config if key != "config_hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def git_sha() -> str:
    env = os.environ.get("GITHUB_SHA") or os.environ.get("GIT_SHA")
    if env:
        return env
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def load_prereg(prereg_dir: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not prereg_dir.exists():
        return found
    for path in sorted(prereg_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text())
        if not isinstance(payload, dict):
            continue
        parsed = PreregConfig.model_validate(payload)
        found[config_hash(payload)] = payload
        found[parsed.prereg_id] = payload
    return found


@dataclass
class HeldOutUnlock:
    who: str
    why: str
    as_of: datetime


@dataclass
class LedgerEntry:
    kind: str
    payload: dict[str, Any]


@dataclass
class BacktestResult:
    config_hash: str
    prereg_id: str
    git_sha: str
    snapshot_id: str
    seed: int
    universe: tuple[str, ...]
    fills: tuple[Fill, ...]
    pnl: Money
    coverage: CoverageReport | None
    extra: dict[str, Any] = field(default_factory=dict)

    def canonical_bytes(self) -> bytes:
        if self.coverage is None:
            raise CoverageReportMissing("backtest result without coverage report is invalid")
        payload = {
            "config_hash": self.config_hash,
            "prereg_id": self.prereg_id,
            "git_sha": self.git_sha,
            "snapshot_id": self.snapshot_id,
            "seed": self.seed,
            "universe": list(self.universe),
            "pnl": str(self.pnl.amount),
            "n_fills": len(self.fills),
            "fill_status": [fill.status for fill in self.fills],
            "fill_qty": [fill.filled_qty for fill in self.fills],
            "coverage": {
                "n_bracket_days": self.coverage.n_bracket_days,
                "n_no_book": self.coverage.n_no_book,
                "n_no_two_sided": self.coverage.n_no_two_sided,
                "n_volume_missing": self.coverage.n_volume_missing,
                "n_volume_zero": self.coverage.n_volume_zero,
                "n_no_ask_size": self.coverage.n_no_ask_size,
                "staleness_seconds": list(self.coverage.staleness_seconds()),
            },
            "extra": self.extra,
        }
        return canonical_json(payload)


class Ledger:
    """Append-only in-process ledger. Unlock entries are permanent and loud."""

    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.entries.append(LedgerEntry(kind=kind, payload=payload))

    def unlock_held_out(self, unlock: HeldOutUnlock) -> None:
        self.record(
            "HELD_OUT_UNLOCK",
            {
                "who": unlock.who,
                "why": unlock.why,
                "as_of": require_utc(unlock.as_of).isoformat(),
                "loud": True,
            },
        )


def refuse_unless_preregistered(config: Mapping[str, Any], prereg_dir: Path) -> dict[str, Any]:
    digest = config_hash(config)
    catalog = load_prereg(prereg_dir)
    if digest not in catalog and str(config.get("prereg_id", "")) not in catalog:
        raise ConfigNotPreregistered(
            f"config hash {digest} / prereg_id {config.get('prereg_id')!r} not in {prereg_dir}"
        )
    registered = catalog.get(digest) or catalog[str(config.get("prereg_id"))]
    return registered


def refuse_if_held_out_locked(
    config: Mapping[str, Any],
    registered: Mapping[str, Any],
    *,
    climate_days: tuple[date, ...],
    unlock: HeldOutUnlock | None,
    ledger: Ledger,
) -> None:
    held = registered.get("held_out") or config.get("held_out") or {}
    if not held or not held.get("locked", True):
        return
    start = date.fromisoformat(str(held["start"])) if held.get("start") else None
    end = date.fromisoformat(str(held["end"])) if held.get("end") else None
    overlap = [
        day
        for day in climate_days
        if (start is None or day >= start) and (end is None or day <= end)
    ]
    if not overlap:
        return
    if unlock is None:
        raise HeldOutLockedError(
            f"held-out {start}..{end} locked by default; pass HeldOutUnlock(who, why) to unlock"
        )
    ledger.unlock_held_out(unlock)
