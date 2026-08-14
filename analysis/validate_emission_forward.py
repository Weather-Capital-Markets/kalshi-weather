"""Forward quote-emission cross-check: VPS logger books vs live API candles.

Allowed DB: none. Reads orderbook JSONL from --data-dir only; never opens
heartbeat.sqlite.

Compares carry-forward top-of-book at each minute boundary against candle
yes_bid/yes_ask closes, and counts book changes in the logger that produced no
candle — the quantity K1 v3 cares about for quote-only emission.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analysis.orderbook import top_of_book_from_payload
from analysis.spread_census import candle_fields, parse_iso_utc
from ingestion.client import KalshiClient
from ingestion.config_loader import load_config
from ingestion.writer import read_jsonl_gz

logger = logging.getLogger(__name__)
PERIOD_SEC = 60
PRICE_TOLERANCE = 0.005


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


def book_at_or_before(
    rows: list[tuple[datetime, float | None, float | None]], boundary: datetime
) -> tuple[float | None, float | None]:
    eligible = [row for row in rows if row[0] <= boundary]
    if not eligible:
        return None, None
    _, bid, ask = eligible[-1]
    return bid, ask


def _prices_match(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= PRICE_TOLERANCE


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
    result = client.get(path, params=params)
    if not result.ok or not isinstance(result.json_body, dict):
        return []
    candles = result.json_body.get("candlesticks")
    if not isinstance(candles, list):
        return []
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
    candle_by_ts = {int(c["end_period_ts"]): c for c in candles}
    candle_ts_set = set(candle_by_ts)
    mismatches: list[dict[str, Any]] = []
    silent_changes: list[dict[str, Any]] = []
    compared = 0
    matched = 0

    previous: tuple[float | None, float | None] | None = None
    for captured, bid, ask in logger_rows:
        current = (bid, ask)
        if previous is not None and current != previous:
            boundary = int(captured.timestamp())
            boundary -= boundary % PERIOD_SEC
            if boundary not in candle_ts_set:
                silent_changes.append(
                    {
                        "ticker": ticker,
                        "ts_utc": captured.isoformat(),
                        "boundary": boundary,
                        "bid": bid,
                        "ask": ask,
                    }
                )
        previous = current

    for boundary in boundaries:
        candle = candle_by_ts.get(boundary)
        if candle is None:
            continue
        boundary_dt = datetime.fromtimestamp(boundary, tz=timezone.utc)
        logger_bid, logger_ask = book_at_or_before(logger_rows, boundary_dt)
        if logger_bid is None and logger_ask is None:
            continue
        compared += 1
        bid_ok = _prices_match(logger_bid, candle.get("bid_close"))
        ask_ok = _prices_match(logger_ask, candle.get("ask_close"))
        if bid_ok and ask_ok:
            matched += 1
        else:
            mismatches.append(
                {
                    "ticker": ticker,
                    "end_period_ts": boundary,
                    "logger_bid": logger_bid,
                    "logger_ask": logger_ask,
                    "candle_bid": candle.get("bid_close"),
                    "candle_ask": candle.get("ask_close"),
                }
            )

    return {
        "ticker": ticker,
        "compared": compared,
        "matched": matched,
        "mismatches": mismatches,
        "silent_changes": silent_changes,
    }


def run(
    *,
    config: dict[str, Any],
    data_dir: Path,
    start: date,
    end: date,
    markets: list[str] | None,
    client: KalshiClient | None = None,
) -> int:
    days = _date_range(start, end)
    if len(days) < 3:
        print("need at least 3 days in range")
        return 1
    discovered = discover_tickers(data_dir, days)
    tickers = markets or discovered
    if len(tickers) < 3:
        print(f"need at least 3 markets with orderbook data; found {len(tickers)}")
        return 1

    selected = set(tickers[: max(3, len(tickers))])
    books = load_logger_books(data_dir, start=start, end=end, tickers=selected)
    active = [t for t in tickers if t in books and books[t]]
    if len(active) < 3:
        print(f"need at least 3 markets with logger books; found {len(active)}")
        return 1

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
        for ticker in active[: max(3, len(active))]:
            rows = books[ticker]
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
    finally:
        if own_client:
            api.close()

    match_rate = (total_matched / total_compared) if total_compared else 0.0
    print(f"markets={min(len(active), 3)} days={len(days)} boundaries_compared={total_compared}")
    print(f"logger_vs_candle_match_rate={match_rate:.4%} mismatches={len(all_mismatches)}")
    if all_mismatches:
        print("sample mismatches:")
        for row in all_mismatches[:5]:
            print(row)
    print(f"silent_book_changes_without_candle={len(all_silent)}")
    if all_silent:
        print("sample silent changes:")
        for row in all_silent[:5]:
            print(row)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward quote-emission cross-check")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--markets", type=str, default=None, help="Comma-separated tickers")
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
    return run(
        config=config,
        data_dir=args.data_dir,
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        markets=markets,
    )


if __name__ == "__main__":
    raise SystemExit(main())
