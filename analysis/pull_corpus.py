"""Pull the KXHIGHNY trade corpus: enumerate markets, merge live + historical.

Network and wall clock are allowed here. This is an ingestion script, not a
backtest path: it writes parquet shards and a merge report and nothing else.

Reads ``GET /historical/cutoff`` first and never assumes the partition. Every
ticker is pulled from both sides of it and merged by ``trade_id`` so a market
straddling the boundary is not silently truncated.

Must never
    Assume the cutoff. Derive direction. Drop the complement assertion.
    Write into ``knowledge/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    HistoricalCutoff,
    RawTrade,
    fetch_cutoff,
    merge_trades,
    parse_climate_day,
    pull_ticker,
    trades_to_parquet,
)
from wxmm.core.errors import PriceComplementError

LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HISTORICAL_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES = ("KXHIGHNY",)
HIGHNY_NEEDLES = ("HIGHNY", "KXHIGHNY")


class HttpTransport:
    """Injected HTTP for ``TradesTransport``. Historical paths use the external host."""

    def __init__(
        self,
        *,
        live_base: str = LIVE_BASE,
        historical_base: str = HISTORICAL_BASE,
        timeout: float = 45.0,
        max_retries: int = 5,
        min_interval: float = 0.0,
    ) -> None:
        self.live_base = live_base.rstrip("/")
        self.historical_base = historical_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.n_requests = 0

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self.historical_base if path.startswith("/historical") else self.live_base
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            query = "?" + urllib.parse.urlencode(clean)
        url = f"{base}{path}{query}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "wxmm-corpus/1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                with self._lock:
                    self.n_requests += 1
                if not isinstance(body, dict):
                    raise ValueError(f"{url} did not return an object")
                return body
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {"trades": [], "markets": [], "cursor": ""}
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last_error = exc
            time.sleep(min(2.0**attempt, 30.0))
        raise RuntimeError(f"GET {url} failed after {self.max_retries} attempts: {last_error}")


def looks_like_highny(ticker: str) -> bool:
    upper = ticker.upper()
    return any(needle in upper for needle in HIGHNY_NEEDLES)


def _paginate_markets(
    transport: HttpTransport,
    path: str,
    series: str,
    *,
    limit: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    cursor: str | None = None
    out: list[dict[str, Any]] = []
    for _ in range(max_pages):
        params: dict[str, Any] = {"series_ticker": series, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = transport.get_json(path, params)
        markets = body.get("markets") or []
        if not isinstance(markets, list):
            raise ValueError(f"{path} markets field is not a list")
        out.extend(m for m in markets if isinstance(m, dict))
        cursor = body.get("cursor") or None
        if not cursor or not markets:
            break
    return out


def enumerate_markets(
    transport: HttpTransport,
    series: str,
    *,
    limit: int = 1000,
    max_pages: int = 200,
) -> list[dict[str, Any]]:
    """Union both sides of the cutoff.

    ``/historical/markets`` reaches the deep archive but stops at the same
    cutoff the trades endpoint does, so on its own it silently drops every
    market opened after it. ``/markets`` holds those and nothing older. The
    enumeration has the same boundary hole the trade merge does, and gets the
    same treatment.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in ("/historical/markets", "/markets"):
        for market in _paginate_markets(
            transport, path, series, limit=limit, max_pages=max_pages
        ):
            ticker = str(market.get("ticker") or "")
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            out.append(market)
    return out


def in_scope(
    ticker: str,
    *,
    start: date,
    end: date,
) -> date | None:
    if not looks_like_highny(ticker):
        return None
    try:
        climate = parse_climate_day(ticker)
    except ValueError:
        return None
    if climate < start or climate > end:
        return None
    return climate


def shard_key(climate: date) -> str:
    return f"{climate.year:04d}-{climate.month:02d}"


def pull_one(
    transport: HttpTransport,
    ticker: str,
    cutoff: HistoricalCutoff,
    *,
    limit: int,
) -> tuple[list[RawTrade], dict[str, Any]]:
    historical, live = pull_ticker(transport, ticker, cutoff, limit=limit)
    merged, report = merge_trades(historical, live, cutoff)
    row = asdict(report)
    row["ticker"] = ticker
    return merged, row


def write_shards(
    by_shard: dict[str, list[RawTrade]],
    out_dir: Path,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for shard, trades in sorted(by_shard.items()):
        if not trades:
            continue
        ordered = sorted(trades, key=lambda t: (t.created_time, t.trade_id))
        trades_to_parquet(ordered, out_dir / f"climate_month={shard}.parquet")
        counts[shard] = len(ordered)
    return counts


def run(
    *,
    series: Iterable[str],
    start: date,
    end: date,
    out_dir: Path,
    workers: int,
    limit: int,
    min_interval: float,
    progress_every: int,
) -> int:
    transport = HttpTransport(min_interval=min_interval)
    cutoff = fetch_cutoff(transport)
    cutoff_dt = datetime.fromtimestamp(cutoff.market_settled_ts, tz=timezone.utc)
    print(f"cutoff market_settled_ts={cutoff.market_settled_ts} ({cutoff_dt.isoformat()})")

    scoped: dict[str, date] = {}
    for name in series:
        markets = enumerate_markets(transport, name)
        print(f"series {name}: {len(markets)} markets in archive")
        for market in markets:
            ticker = str(market.get("ticker") or "")
            climate = in_scope(ticker, start=start, end=end)
            if climate is not None:
                scoped[ticker] = climate
    tickers = sorted(scoped)
    print(f"in scope {start} .. {end}: {len(tickers)} tickers")
    if not tickers:
        print("nothing to pull", file=sys.stderr)
        return 1

    by_shard: dict[str, list[RawTrade]] = defaultdict(list)
    reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    lock = threading.Lock()
    done = 0
    started = time.monotonic()

    def work(ticker: str) -> None:
        nonlocal done
        try:
            merged, report = pull_one(transport, ticker, cutoff, limit=limit)
        except (RuntimeError, ValueError, PriceComplementError, KeyError) as exc:
            with lock:
                failures.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
                done += 1
            return
        shard = shard_key(scoped[ticker])
        with lock:
            by_shard[shard].extend(merged)
            reports.append(report)
            done += 1
            if progress_every and done % progress_every == 0:
                rate = done / max(time.monotonic() - started, 1e-9)
                total = sum(len(v) for v in by_shard.values())
                print(
                    f"  {done}/{len(tickers)} tickers  {total} trades  "
                    f"{rate:.1f} tick/s  {len(failures)} failed",
                    flush=True,
                )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, tickers))

    counts = write_shards(by_shard, out_dir)
    total = sum(counts.values())
    meta = {
        "pulled_at": datetime.now(tz=timezone.utc).isoformat(),
        "series": list(series),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cutoff_market_settled_ts": cutoff.market_settled_ts,
        "cutoff_iso": cutoff_dt.isoformat(),
        "n_tickers_scoped": len(tickers),
        "n_tickers_with_trades": len(reports),
        "n_failures": len(failures),
        "failures": failures[:200],
        "n_trades": total,
        "shards": counts,
        "n_requests": transport.n_requests,
        "n_duplicate_ids": sum(int(r["n_duplicate_ids"]) for r in reports),
        "n_boundary_overlap": sum(int(r["n_boundary_overlap"]) for r in reports),
        "gap_notes": [r["gap_note"] for r in reports if r["gap_note"]][:50],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "merge_report.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k != "failures"}, indent=2))
    return 0 if total else 1


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", nargs="+", default=list(DEFAULT_SERIES))
    parser.add_argument("--start", type=_parse_date, default=SIX_BRACKET_ERA_START)
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument("--out", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--min-interval", type=float, default=0.0)
    parser.add_argument("--progress-every", type=int, default=250)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        series=args.series,
        start=args.start,
        end=args.end,
        out_dir=args.out,
        workers=args.workers,
        limit=args.limit,
        min_interval=args.min_interval,
        progress_every=args.progress_every,
    )


if __name__ == "__main__":
    raise SystemExit(main())
