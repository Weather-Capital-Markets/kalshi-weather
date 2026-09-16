"""Forward quote-emission cross-check: VPS logger books vs live API candles.

Allowed DB: none. Reads orderbook JSONL from --data-dir only; never opens
heartbeat.sqlite.

Compares carry-forward top-of-book at each minute boundary against candle
yes_bid/yes_ask closes, and counts book changes in the logger that produced no
candle — the quantity K1 v3 cares about for quote-only emission.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analysis.emission_compare import compare_ticker_convention
from analysis.orderbook import top_of_book_from_payload
from analysis.spread_census import candle_fields, parse_iso_utc
from ingestion.client import KalshiClient, RequestResult
from ingestion.config_loader import load_config
from ingestion.writer import read_jsonl_gz

logger = logging.getLogger(__name__)
PERIOD_SEC = 60
MIN_DAYS = 3
MIN_MARKETS = 3


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _date_range(start: date, end: date) -> list[str]:
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def load_logger_books(
    data_dir: Path,
    *,
    start: date,
    end: date,
    tickers: set[str] | None = None,
) -> dict[str, list[tuple[datetime, float | None, float | None]]]:
    """Return {ticker: [(ts_utc, bid, ask), ...]} in capture order."""
    by_ticker: dict[str, list[tuple[datetime, float | None, float | None]]] = defaultdict(list)
    for day in _date_range(start, end):
        pattern = data_dir / day / "orderbook"
        if not pattern.is_dir():
            continue
        for path in sorted(pattern.glob("*.jsonl.gz")):
            ticker = path.name.removesuffix(".jsonl.gz")
            if tickers is not None and ticker not in tickers:
                continue
            for record in read_jsonl_gz(path):
                ts_raw = record.get("ts_utc")
                payload = record.get("payload")
                if not isinstance(ts_raw, str) or not isinstance(payload, dict):
                    continue
                captured = parse_iso_utc(ts_raw)
                if captured is None:
                    continue
                bid, ask = top_of_book_from_payload(payload)
                by_ticker[ticker].append((captured, bid, ask))
    for ticker in by_ticker:
        by_ticker[ticker].sort(key=lambda row: row[0])
    return dict(by_ticker)


def minute_boundaries(start: datetime, end: datetime) -> list[int]:
    """Inclusive end_period_ts values at 60s boundaries within [start, end]."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("boundaries require aware datetimes")
    first = int(start.timestamp())
    first -= first % PERIOD_SEC
    last = int(end.timestamp())
    last -= last % PERIOD_SEC
    return list(range(first, last + 1, PERIOD_SEC))


def discover_tickers(data_dir: Path, days: list[str]) -> list[str]:
    tickers: set[str] = set()
    for day in days:
        folder = data_dir / day / "orderbook"
        if not folder.is_dir():
            continue
        tickers.update(path.name.removesuffix(".jsonl.gz") for path in folder.glob("*.jsonl.gz"))
    return sorted(tickers)


class EmissionValidationError(Exception):
    """API or parse failure during forward emission validation."""


def fetch_candles(
    client: KalshiClient,
    ticker: str,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    series = ticker.split("-", 1)[0]
    path = client.path("candlesticks", series_ticker=series, ticker=ticker)
    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 1,
    }
    result: RequestResult = client.get(path, params=params)
    if not result.ok:
        raise EmissionValidationError(
            f"candlesticks fetch failed for {ticker}: status={result.status_code} "
            f"error={result.error_text!r}"
        )
    if not isinstance(result.json_body, dict):
        raise EmissionValidationError(
            f"candlesticks response for {ticker} is not a JSON object"
        )
    candles = result.json_body.get("candlesticks")
    if not isinstance(candles, list):
        raise EmissionValidationError(
            f"candlesticks response for {ticker} missing candlesticks list"
        )
    parsed = [candle_fields(c) for c in candles if isinstance(c, dict)]
    parsed.sort(key=lambda c: int(c["end_period_ts"]))
    return parsed


def compare_ticker(
    *,
    ticker: str,
    logger_rows: list[tuple[datetime, float | None, float | None]],
    candles: list[dict[str, Any]],
    boundaries: list[int],
) -> dict[str, Any]:
    """Legacy default: interval_start | UTC | offset 0 (prose baseline convention)."""
    return compare_ticker_convention(
        ticker=ticker,
        logger_rows=logger_rows,
        candles=candles,
        boundaries=boundaries,
        interval_anchor="interval_start",
        timezone_mode="UTC",
        bucket_offset=0,
    )


def _write_report(out_dir: Path, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "emission_forward.txt"
    json_path = out_dir / "emission_forward.json"
    lines = [
        "forward quote-emission cross-check (measurement only)",
        f"days_available={report['days_available']} required_days={MIN_DAYS}",
        f"markets_discovered={report['markets_discovered']}",
        f"markets_with_books={report['markets_with_books']}",
        f"markets_compared={report['markets_compared']}",
        f"boundaries_compared={report['boundaries_compared']}",
        f"logger_vs_candle_match_rate={report['match_rate']:.6f}",
        f"mismatches={report['mismatch_count']}",
        f"silent_book_changes_without_candle={report['silent_count']}",
    ]
    if report.get("shortfall_notes"):
        lines.extend(report["shortfall_notes"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return txt_path


def run(
    *,
    config: dict[str, Any],
    data_dir: Path,
    start: date,
    end: date,
    markets: list[str] | None,
    client: KalshiClient | None = None,
    out_dir: Path | None = None,
) -> int:
    days = _date_range(start, end)
    discovered = discover_tickers(data_dir, days)
    tickers = markets or discovered
    books = load_logger_books(data_dir, start=start, end=end, tickers=set(tickers))
    active = [t for t in tickers if t in books and books[t]]

    shortfall_notes: list[str] = []
    if len(days) < MIN_DAYS:
        shortfall_notes.append(
            f"SHORTFALL: need {MIN_DAYS} days in range, have {len(days)} — reporting available span"
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
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)
    boundaries = minute_boundaries(start_dt, end_dt)

    total_compared = 0
    total_matched = 0
    all_mismatches: list[dict[str, Any]] = []
    all_silent: list[dict[str, Any]] = []

    try:
        for ticker in compare_tickers:
            rows = books.get(ticker, [])
            if not rows:
                continue
            start_ts = int(rows[0][0].timestamp()) - PERIOD_SEC
            end_ts = int(rows[-1][0].timestamp()) + PERIOD_SEC
            candles = fetch_candles(api, ticker, start_ts, end_ts)
            result = compare_ticker(
                ticker=ticker,
                logger_rows=rows,
                candles=candles,
                boundaries=boundaries,
            )
            total_compared += int(result["compared"])
            total_matched += int(result["matched"])
            all_mismatches.extend(result["mismatches"])
            all_silent.extend(result["silent_changes"])
    except EmissionValidationError as exc:
        print(f"API_FAILURE: {exc}")
        return 4
    finally:
        if own_client:
            api.close()

    match_rate = (total_matched / total_compared) if total_compared else 0.0
    report = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_available": len(days),
        "markets_discovered": len(tickers),
        "markets_with_books": len(active),
        "markets_compared": len(compare_tickers),
        "tickers_compared": compare_tickers,
        "boundaries_compared": total_compared,
        "match_rate": match_rate,
        "mismatch_count": len(all_mismatches),
        "silent_count": len(all_silent),
        "shortfall_notes": shortfall_notes,
        "sample_mismatches": all_mismatches[:5],
        "sample_silent_changes": all_silent[:5],
    }

    print(
        f"days={len(days)} markets_discovered={len(tickers)} "
        f"markets_compared={len(compare_tickers)}"
    )
    print(f"boundaries_compared={total_compared}")
    print(f"logger_vs_candle_match_rate={match_rate:.4%} mismatches={len(all_mismatches)}")
    for note in shortfall_notes:
        print(note)
    if all_mismatches:
        print("sample mismatches:")
        for row in all_mismatches[:5]:
            print(row)
    print(f"silent_book_changes_without_candle={len(all_silent)}")
    if all_silent:
        print("sample silent changes:")
        for row in all_silent[:5]:
            print(row)

    if out_dir is not None:
        path = _write_report(out_dir, report)
        print(f"wrote {path}")

    if shortfall_notes:
        return 2
    if total_compared == 0:
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward quote-emission cross-check")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--markets", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    if args.markets:
        markets = [part.strip() for part in args.markets.split(",") if part.strip()]
    else:
        markets = None
    emission_cfg = config.get("validate_emission_forward") or {}
    out_dir = args.out_dir or Path(str(emission_cfg.get("out_dir") or "analysis/out"))
    return run(
        config=config,
        data_dir=args.data_dir,
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        markets=markets,
        out_dir=out_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
