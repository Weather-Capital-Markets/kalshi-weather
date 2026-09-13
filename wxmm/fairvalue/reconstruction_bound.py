"""Phase 3 reconstruction error bound. ``model.fit`` refuses without this.

Allowed to assume
    ``analysis/out/emission_convention_sweep.json`` exists after Stage 0 and
    names the winning join convention plus match_rate / silent_count.

Must never
    Invent a bound. Fit with a missing or stale bound file. Merge bound with
    ``SIZE_UNKNOWN``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wxmm.core.errors import ReconstructionBoundRequired
from wxmm.core.utc import require_utc

DEFAULT_BOUND_PATH = Path("analysis/out/emission_convention_sweep.json")


@dataclass(frozen=True, slots=True)
class ReconstructionBound:
    """Named error bound from the 12-way emission convention sweep."""

    branch: str  # "full_corpus" | "forward_logger_only"
    interval_anchor: str
    timezone: str
    bucket_offset: int
    match_rate: float
    silent_count: int
    boundaries_compared: int
    inputs_manifest: str
    measured_at: datetime
    winning_convention_found: bool

    @property
    def error_bound_note(self) -> str:
        return (
            f"match_rate={self.match_rate:.6f} silent={self.silent_count} "
            f"n={self.boundaries_compared} branch={self.branch}"
        )


def load_reconstruction_bound(path: Path | None = None) -> ReconstructionBound:
    """Load the committed sweep artifact. Raises if missing or incomplete."""
    bound_path = path or DEFAULT_BOUND_PATH
    if not bound_path.is_file():
        raise ReconstructionBoundRequired(
            f"reconstruction bound missing at {bound_path}; run Stage 0 emission sweep"
        )
    raw = json.loads(bound_path.read_text(encoding="utf-8"))
    winner = raw.get("winner")
    if not isinstance(winner, dict):
        raise ReconstructionBoundRequired(
            f"{bound_path} has no winner row; Stage 0 sweep incomplete"
        )
    branch = str(raw.get("branch") or "")
    if branch not in {"full_corpus", "forward_logger_only"}:
        raise ReconstructionBoundRequired(
            f"{bound_path} branch must be full_corpus|forward_logger_only, not {branch!r}"
        )
    measured_raw = raw.get("measured_at")
    if not isinstance(measured_raw, str):
        raise ReconstructionBoundRequired(f"{bound_path} missing measured_at")
    return ReconstructionBound(
        branch=branch,
        interval_anchor=str(winner["interval_anchor"]),
        timezone=str(winner["timezone"]),
        bucket_offset=int(winner["bucket_offset"]),
        match_rate=float(winner["match_rate"]),
        silent_count=int(winner["silent_count"]),
        boundaries_compared=int(winner["boundaries_compared"]),
        inputs_manifest=str(raw.get("inputs_manifest") or ""),
        measured_at=require_utc(datetime.fromisoformat(measured_raw.replace("Z", "+00:00"))),
        winning_convention_found=bool(raw.get("winning_convention_found")),
    )


def require_reconstruction_bound_for_fit(path: Path | None = None) -> ReconstructionBound:
    """Hard gate for ``model.fit`` and market-mid baseline. Refuses, never warns."""
    return load_reconstruction_bound(path)
