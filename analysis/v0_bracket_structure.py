"""Phase B5 bracket-structure reporter for v0 RUN."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from analysis.bracket_enumeration import event_structure
from analysis.spread_census import ticker_climate_date
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START

EXPECTED_SIX_BRACKET_START = SIX_BRACKET_ERA_START


def _load_markets(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list of markets")
    return [row for row in payload if isinstance(row, dict)]


def bracket_structure_report(markets_path: Path | str) -> dict[str, Any]:
    """Confirm six-bracket era start and list post-era days with bracket count != 6."""
    markets = _load_markets(Path(markets_path))
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        climate_date = ticker_climate_date(str(market.get("ticker") or ""))
        if climate_date is None:
            continue
        by_day[climate_date].append(market)

    daily_rows: list[dict[str, Any]] = []
    for climate_date in sorted(by_day):
        row = event_structure(climate_date, by_day[climate_date])
        if row is not None:
            daily_rows.append(row)

    six_bracket_days = [
        row
        for row in daily_rows
        if date.fromisoformat(row["climate_date"]) >= EXPECTED_SIX_BRACKET_START
    ]
    observed_start: str | None = None
    for row in six_bracket_days:
        if row["n_brackets"] == 6:
            observed_start = row["climate_date"]
            break

    non_six_after_start = [
        {
            "climate_date": row["climate_date"],
            "n_brackets": row["n_brackets"],
            "structure_id": row["structure_id"],
        }
        for row in six_bracket_days
        if row["n_brackets"] != 6
    ]

    return {
        "expected_six_bracket_era_start": EXPECTED_SIX_BRACKET_START.isoformat(),
        "observed_first_six_bracket_day": observed_start,
        "six_bracket_start_confirmed": observed_start == EXPECTED_SIX_BRACKET_START.isoformat(),
        "n_climate_days_on_or_after_start": len(six_bracket_days),
        "n_days_bracket_count_ne_6": len(non_six_after_start),
        "days_bracket_count_ne_6": non_six_after_start,
        "note": "Write-ready payload for analysis/out/v0_run/bracket_structure.json",
    }
