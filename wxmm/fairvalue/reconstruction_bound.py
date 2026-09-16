"""Phase 3 reconstruction error bound. ``model.fit`` refuses without this.

Allowed to assume
    ``analysis/out/emission_convention_sweep.json`` exists after Stage 0.

Must never
    Treat ``sweep_status != COMPLETE`` as a bound. Invent a bound. Fit from a
    missing, incomplete, or transcribed-only artifact. Merge with ``SIZE_UNKNOWN``.
    Infer ``branch`` from an incomplete sweep — branch is a finding, absent until
    COMPLETE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wxmm.core.errors import ReconstructionBoundRequired
from wxmm.core.utc import require_utc

DEFAULT_BOUND_PATH = Path("analysis/out/emission_convention_sweep.json")
REQUIRED_ROWS = 12


@dataclass(frozen=True, slots=True)
class ReconstructionBound:
    """Named error bound from a COMPLETE 12-way emission convention sweep."""

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
    reconstruction_error_bound: str
    sweep_status: str

    @property
    def error_bound_note(self) -> str:
        return (
            f"bound={self.reconstruction_error_bound!r} "
            f"match_rate={self.match_rate:.6f} silent={self.silent_count} "
            f"n={self.boundaries_compared} branch={self.branch}"
        )


def _row_is_computed(row: dict[str, Any]) -> bool:
    if row.get("provenance") == "transcribed_not_computed":
        return False
    if row.get("row_status") in {"not_computed", "transcribed_not_computed"}:
        return False
    if row.get("pending_vps_rerun") is True:
        return False
    compared = row.get("boundaries_compared")
    return isinstance(compared, int) and compared > 0


def load_reconstruction_bound(path: Path | None = None) -> ReconstructionBound:
    """Load a COMPLETE sweep artifact. Raises on missing or incomplete Stage 0."""
    bound_path = path or DEFAULT_BOUND_PATH
    if not bound_path.is_file():
        raise ReconstructionBoundRequired(
            f"reconstruction bound missing at {bound_path}; run Stage 0 emission sweep"
        )
    raw = json.loads(bound_path.read_text(encoding="utf-8"))

    sweep_status = str(raw.get("sweep_status") or "")
    if sweep_status != "COMPLETE":
        raise ReconstructionBoundRequired(
            f"{bound_path} sweep_status={sweep_status!r}; "
            "require COMPLETE before fit or market-mid (branch is not available yet)"
        )

    table = raw.get("table")
    if not isinstance(table, list) or len(table) != REQUIRED_ROWS:
        raise ReconstructionBoundRequired(
            f"{bound_path} must contain exactly {REQUIRED_ROWS} convention rows"
        )
    computed = [row for row in table if isinstance(row, dict) and _row_is_computed(row)]
    if len(computed) != REQUIRED_ROWS:
        raise ReconstructionBoundRequired(
            f"{bound_path} rows_computed={len(computed)}/{REQUIRED_ROWS}; "
            "all twelve diagnostic conventions must be computed"
        )
    rows_computed = str(raw.get("rows_computed") or "")
    if rows_computed not in {f"{REQUIRED_ROWS}/{REQUIRED_ROWS}", "12/12"}:
        raise ReconstructionBoundRequired(
            f"{bound_path} rows_computed must be 12/12 when COMPLETE, not {rows_computed!r}"
        )

    bound_name = raw.get("reconstruction_error_bound")
    if not isinstance(bound_name, str) or not bound_name.strip():
        raise ReconstructionBoundRequired(
            f"{bound_path} missing named reconstruction_error_bound"
        )

    # Branch is a finding: only readable once COMPLETE (and must then be present).
    if "branch" not in raw:
        raise ReconstructionBoundRequired(
            f"{bound_path} COMPLETE artifact missing branch finding"
        )
    branch = str(raw["branch"])
    if branch not in {"full_corpus", "forward_logger_only"}:
        raise ReconstructionBoundRequired(
            f"{bound_path} branch must be full_corpus|forward_logger_only, not {branch!r}"
        )

    winner = raw.get("winner")
    if not isinstance(winner, dict):
        raise ReconstructionBoundRequired(
            f"{bound_path} has no winner row after COMPLETE sweep"
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
        reconstruction_error_bound=bound_name.strip(),
        sweep_status=sweep_status,
    )


def require_reconstruction_bound_for_fit(path: Path | None = None) -> ReconstructionBound:
    """Hard gate for ``model.fit`` and market-mid baseline. Refuses, never warns."""
    return load_reconstruction_bound(path)
