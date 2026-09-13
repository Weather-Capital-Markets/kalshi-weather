"""Stage 0: 12-way logger-vs-candle convention sweep (B2 Phase 3).

Recomputes match rate and silent-change count for each join convention over data
already captured on the VPS logger. Writes reproducible artifacts under
``analysis/out/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from analysis.emission_bucketing import (
    CONVENTIONS,
    convention_key,
    iter_conventions,
)
from analysis.emission_compare import compare_ticker_convention
from analysis.validate_emission_forward import (
    MIN_DAYS,
    MIN_MARKETS,
    discover_tickers,
    fetch_candles,
    load_logger_books,
    minute_boundaries,
)
from ingestion.client import KalshiClient
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)

MATCH_LOW = 0.60
MATCH_HIGH = 0.90
FEW_PERCENT = 0.05


class EmissionSweepError(Exception):
    """Sweep cannot produce a trustworthy artifact."""


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _rows_computed(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if int(row.get("boundaries_compared") or 0) > 0)


def _branch_from_table(rows: list[dict[str, Any]]) -> tuple[str, bool, dict[str, Any] | None]:
    """Return (branch, winning_convention_found, winner_row).

    Call only when all 12 conventions are computed. Branch is a finding.
    """
    viable = [
        row
        for row in rows
        if MATCH_LOW <= float(row["match_rate"]) <= MATCH_HIGH and int(row["boundaries_compared"]) > 0
    ]
    if viable:
        winner = max(viable, key=lambda row: float(row["match_rate"]))
        return "full_corpus", True, winner
    best = max(rows, key=lambda row: float(row["match_rate"]))
    return "forward_logger_only", False, best


def sweep_conventions(
    *,
    config: dict[str, Any],
    data_dir: Path,
    start: date,
    end: date,
    markets: list[str] | None,
    client: KalshiClient | None = None,
    inputs_note: str | None = None,
) -> dict[str, Any]:
    from datetime import datetime as dt
    from datetime import timedelta

    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)

    discovered = discover_tickers(data_dir, days)
    tickers = markets or discovered
    books = load_logger_books(data_dir, start=start, end=end, tickers=set(tickers))
    active = [t for t in tickers if t in books and books[t]]

    shortfall_notes: list[str] = []
    if len(days) < MIN_DAYS:
        shortfall_notes.append(
            f"SHORTFALL: need {MIN_DAYS} days in range, have {len(days)}"
        )
    if len(tickers) < MIN_MARKETS:
        shortfall_notes.append(
            f"SHORTFALL: need {MIN_MARKETS} markets with orderbook data, have {len(tickers)}"
        )
    if len(active) < MIN_MARKETS:
        shortfall_notes.append(
            f"SHORTFALL: need {MIN_MARKETS} markets with logger books, have {len(active)}"
        )

    compare_tickers = active[: max(MIN_MARKETS, len(active))]
    own_client = client is None
    api = client or KalshiClient(config)
    start_dt = dt.combine(start, dt.min.time(), tzinfo=timezone.utc)
    end_dt = dt.combine(end, dt.max.time(), tzinfo=timezone.utc)
    boundaries = minute_boundaries(start_dt, end_dt)

    candle_cache: dict[str, list[dict[str, Any]]] = {}
    try:
        for ticker in compare_tickers:
            rows = books.get(ticker, [])
            if not rows:
                continue
            start_ts = int(rows[0][0].timestamp()) - 60
            end_ts = int(rows[-1][0].timestamp()) + 60
            candle_cache[ticker] = fetch_candles(api, ticker, start_ts, end_ts)
    finally:
        if own_client:
            api.close()

    table: list[dict[str, Any]] = []
    for interval_anchor, timezone_mode, bucket_offset in iter_conventions():
        total_compared = 0
        total_matched = 0
        all_silent: list[dict[str, Any]] = []
        for ticker in compare_tickers:
            rows = books.get(ticker, [])
            if not rows:
                continue
            candles = candle_cache.get(ticker, [])
            result = compare_ticker_convention(
                ticker=ticker,
                logger_rows=rows,
                candles=candles,
                boundaries=boundaries,
                interval_anchor=interval_anchor,
                timezone_mode=timezone_mode,
                bucket_offset=bucket_offset,
            )
            total_compared += int(result["compared"])
            total_matched += int(result["matched"])
            all_silent.extend(result["silent_changes"])

        match_rate = (total_matched / total_compared) if total_compared else 0.0
        table.append(
            {
                "interval_anchor": interval_anchor,
                "timezone": timezone_mode,
                "bucket_offset": bucket_offset,
                "key": convention_key(interval_anchor, timezone_mode, bucket_offset),
                "boundaries_compared": total_compared,
                "matched": total_matched,
                "match_rate": match_rate,
                "silent_count": len(all_silent),
            }
        )

    computed = _rows_computed(table)
    complete = computed == len(CONVENTIONS) == 12 and not shortfall_notes
    measured_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    manifest = {
        "data_dir": str(data_dir.resolve()),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prose_crosscheck_window": {
            "status": "DECLARE_EXPLICITLY",
            "note": (
                "Original 0.9%/2026 prose window is UNKNOWN_NOT_IN_PROJECT_RECORD. "
                "Record the exact start/end used for this run; do not assume identity "
                "with the prose figures solely because --start defaults to 2026-08-18."
            ),
        },
        "markets_discovered": len(tickers),
        "markets_compared": len(compare_tickers),
        "tickers_compared": compare_tickers,
        "shortfall_notes": shortfall_notes,
        "note": inputs_note or "",
        "input_file_hashes": {},
    }

    report: dict[str, Any] = {
        "measured_at": measured_at,
        "sweep_status": "COMPLETE" if complete else "INCOMPLETE",
        "rows_computed": f"{computed}/12",
        "table": table,
        "inputs_manifest": "analysis/out/emission_convention_sweep_inputs.json",
        "shortfall_notes": shortfall_notes,
        "manifest": manifest,
    }
    # Branch is a finding: absent unless COMPLETE. Never default it.
    if complete:
        branch, winning_found, winner = _branch_from_table(table)
        report["branch"] = branch
        report["winning_convention_found"] = winning_found
        report["winner"] = winner
        report["reconstruction_error_bound"] = (
            f"carry_forward_last_quote@{winner['key']}"
            if winner is not None
            else "carry_forward_last_quote"
        )
    return report


def print_table(table: list[dict[str, Any]]) -> None:
    header = (
        f"{'anchor':<16} {'tz':<4} {'off':>3}  "
        f"{'compared':>10} {'match_rate':>12} {'silent':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in table:
        print(
            f"{row['interval_anchor']:<16} {row['timezone']:<4} {row['bucket_offset']:>+3d}  "
            f"{row['boundaries_compared']:>10} "
            f"{float(row['match_rate']):>11.4%} "
            f"{row['silent_count']:>8}"
        )


def write_artifacts(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "emission_convention_sweep.json"
    manifest_path = out_dir / "emission_convention_sweep_inputs.json"
    payload = {k: v for k, v in report.items() if k != "manifest"}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(report["manifest"], indent=2, default=str),
        encoding="utf-8",
    )
    txt_path = out_dir / "emission_convention_sweep.txt"
    branch = report.get("branch")
    branch_txt = branch if branch is not None else "<absent; not COMPLETE>"
    lines = [
        "emission convention sweep (Stage 0 / B2 Phase 3)",
        f"measured_at={report['measured_at']}",
        f"sweep_status={report['sweep_status']} rows_computed={report['rows_computed']}",
        f"branch={branch_txt} winning_convention_found={report.get('winning_convention_found')}",
        "",
    ]
    if report.get("winner"):
        w = report["winner"]
        lines.append(
            f"winner: {w['key']} match_rate={float(w['match_rate']):.6f} "
            f"silent={w['silent_count']} n={w['boundaries_compared']}"
        )
        if report.get("reconstruction_error_bound"):
            lines.append(f"reconstruction_error_bound={report['reconstruction_error_bound']}")
    lines.append("")
    for row in report["table"]:
        lines.append(
            f"{row['key']}: match_rate={float(row['match_rate']):.6f} "
            f"silent={row['silent_count']} n={row['boundaries_compared']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, manifest_path


def run(
    *,
    config: dict[str, Any],
    data_dir: Path,
    start: date,
    end: date,
    markets: list[str] | None,
    client: KalshiClient | None = None,
    out_dir: Path | None = None,
    inputs_note: str | None = None,
    allow_shortfall: bool = False,
) -> int:
    report = sweep_conventions(
        config=config,
        data_dir=data_dir,
        start=start,
        end=end,
        markets=markets,
        client=client,
        inputs_note=inputs_note,
    )
    print_table(report["table"])
    print(
        f"sweep_status={report['sweep_status']} rows_computed={report['rows_computed']} "
        f"branch={report.get('branch', '<absent>')} "
        f"winning_convention_found={report.get('winning_convention_found')}"
    )
    if report.get("winner"):
        w = report["winner"]
        print(
            f"winner: {w['key']} match_rate={float(w['match_rate']):.4%} "
            f"silent={w['silent_count']}"
        )
    for note in report.get("shortfall_notes", []):
        print(note)

    if out_dir is not None:
        json_path, manifest_path = write_artifacts(report, out_dir)
        print(f"wrote {json_path}")
        print(f"wrote {manifest_path}")

    if report.get("shortfall_notes") and not allow_shortfall:
        return 2
    if not any(int(row["boundaries_compared"]) > 0 for row in report["table"]):
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="12-way emission convention sweep")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--markets", type=str, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    parser.add_argument(
        "--allow-shortfall",
        action="store_true",
        help="Exit 0 even when MIN_DAYS/MIN_MARKETS shortfall (fixture runs)",
    )
    parser.add_argument("--inputs-note", type=str, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    markets = (
        [part.strip() for part in args.markets.split(",") if part.strip()]
        if args.markets
        else None
    )
    return run(
        config=config,
        data_dir=args.data_dir,
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        markets=markets,
        out_dir=args.out_dir,
        inputs_note=args.inputs_note,
        allow_shortfall=args.allow_shortfall,
    )


if __name__ == "__main__":
    raise SystemExit(main())
