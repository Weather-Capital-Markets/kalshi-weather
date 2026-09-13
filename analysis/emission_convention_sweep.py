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
from analysis.emission_decision import decide_from_table
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

# Declared Stage 0 window. Do NOT recover the orphan prose 0.9% window.
# Fresh computed interval_start|UTC|+0 supersedes the transcribed row.
DEFAULT_WINDOW_START = date(2026, 8, 19)


class EmissionSweepError(Exception):
    """Sweep cannot produce a trustworthy artifact."""


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _rows_computed(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if isinstance(row.get("boundaries_compared"), int)
        and int(row["boundaries_compared"]) > 0
        and row.get("provenance") != "transcribed_not_computed"
    )


def _coverage_by_market_day(
    books: dict[str, list[tuple[datetime, float | None, float | None]]],
    candle_cache: dict[str, list[dict[str, Any]]],
    days: list[str],
) -> dict[str, Any]:
    """Per-market and per-day logger/candle presence — not just pooled n."""
    by_market: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, int]] = {
        day: {"markets_with_books": 0, "logger_rows": 0, "candles": 0} for day in days
    }
    for ticker, rows in books.items():
        day_counts: dict[str, int] = {}
        for ts, _bid, _ask in rows:
            day = ts.astimezone(timezone.utc).date().isoformat()
            day_counts[day] = day_counts.get(day, 0) + 1
            if day in by_day:
                by_day[day]["logger_rows"] += 1
        candles = candle_cache.get(ticker, [])
        candle_days: dict[str, int] = {}
        for candle in candles:
            end_ts = int(candle["end_period_ts"])
            day = datetime.fromtimestamp(end_ts, tz=timezone.utc).date().isoformat()
            candle_days[day] = candle_days.get(day, 0) + 1
            if day in by_day:
                by_day[day]["candles"] += 1
        for day, n in day_counts.items():
            if day in by_day and n > 0:
                by_day[day]["markets_with_books"] += 1
        by_market[ticker] = {
            "logger_rows": len(rows),
            "candles": len(candles),
            "days_with_books": sorted(day_counts),
            "days_with_candles": sorted(candle_days),
        }
    return {"by_market": by_market, "by_day": by_day}


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
                "row_status": "computed",
                "provenance": "computed",
            }
        )

    coverage = _coverage_by_market_day(books, candle_cache, days)
    computed = _rows_computed(table)
    complete = computed == len(CONVENTIONS) == 12 and not shortfall_notes
    measured_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    decision = decide_from_table(table)
    manifest = {
        "data_dir": str(data_dir.resolve()),
        "declared_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "policy": (
                "Declared Stage 0 window. Suggested start DEFAULT_WINDOW_START="
                f"{DEFAULT_WINDOW_START.isoformat()} through latest complete climate day. "
                "Do not recover the orphan prose 0.9% window — it is unreproducible."
            ),
        },
        "orphan_prose_figures": {
            "match_rate": 0.009,
            "silent_count": 2026,
            "status": "UNREPRODUCIBLE_ABANDONED",
            "note": (
                "Transcribed from project-record prose only. Window unknown. "
                "n=225111 was confabulated as round(2026/0.009) treating silent_count "
                "as if it were a match count; withdrawn. Fresh computed "
                "interval_start|UTC|+0 on this declared window supersedes the "
                "transcribed row; transcribed figures stay in this manifest only."
            ),
        },
        "markets_discovered": len(tickers),
        "markets_compared": len(compare_tickers),
        "tickers_compared": compare_tickers,
        "coverage": coverage,
        "shortfall_notes": shortfall_notes,
        "note": inputs_note or "",
        "input_file_hashes": {},
        "wellposedness_gate": (
            "If decision.kind=LOW, run analysis/emission_wellposedness.py before "
            "concluding emission-on-change falsified."
        ),
    }

    # Computed table only — no transcribed placeholder rows once a run produces numbers.
    report: dict[str, Any] = {
        "measured_at": measured_at,
        "sweep_status": "COMPLETE" if complete else "INCOMPLETE",
        "rows_computed": f"{computed}/12",
        "decision": {
            "kind": decision.kind,
            "max_match_rate": decision.max_match_rate,
            "winning_key": decision.winning_key,
            "note": decision.note,
        },
        "table": table,
        "inputs_manifest": "analysis/out/emission_convention_sweep_inputs.json",
        "shortfall_notes": shortfall_notes,
        "manifest": manifest,
    }
    # Branch is a finding: only JOIN_BUG may set it, and only when COMPLETE.
    if complete and decision.kind == "JOIN_BUG":
        winner = next(row for row in table if row["key"] == decision.winning_key)
        report["branch"] = decision.branch
        report["winning_convention_found"] = True
        report["winner"] = winner
        report["reconstruction_error_bound"] = decision.reconstruction_error_bound
    elif complete:
        report["winning_convention_found"] = False
        if decision.winning_key is not None:
            report["winner"] = next(
                row for row in table if row["key"] == decision.winning_key
            )
        # branch intentionally absent for PARTIAL / LOW
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
        f"decision={report.get('decision', {}).get('kind')} "
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
    parser.add_argument(
        "--start",
        type=str,
        default=DEFAULT_WINDOW_START.isoformat(),
        help=f"Declared window start (default {DEFAULT_WINDOW_START.isoformat()}; do not recover orphan prose window)",
    )
    parser.add_argument(
        "--end",
        type=str,
        required=True,
        help="Declared window end: latest complete climate day on the VPS logger",
    )
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
