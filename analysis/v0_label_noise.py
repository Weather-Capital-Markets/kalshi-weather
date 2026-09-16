"""Phase B4 label-noise reporter (thin wrapper over v0_settlement_audit)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from analysis.v0_settlement_audit import label_noise_report as _label_noise_report
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START

__all__ = ["label_noise_report"]


def label_noise_report(
    clinyc_path: Path | str,
    *,
    start: date = SIX_BRACKET_ERA_START,
    end: date | None = None,
) -> dict[str, Any]:
    return _label_noise_report(clinyc_path, start=start, end=end)
