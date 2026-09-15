"""Pull the KXHIGHNY/HIGHNY trade corpus: enumerate markets, merge live + historical.

Network and wall clock are allowed here. This is an ingestion script, not a
backtest path: it writes parquet shards and a merge report and nothing else.

Reads ``GET /historical/cutoff`` first and never assumes the partition. Every
ticker is pulled from both sides of it and merged by ``trade_id`` so a market
straddling the boundary is not silently truncated.

Must never
    Assume the cutoff. Derive direction. Drop a ticker on a complement miss.
    Write into ``knowledge/``.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
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
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlparse

from wxmm.analysis.raw_store import CheckpointStore, ContentAddressedRawStore
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    ComplementTally,
    HistoricalCutoff,
    RawTrade,
    fetch_cutoff,
    merge_trades,
    parse_climate_day,
    pull_ticker,
    read_trades_parquet,
    trades_to_parquet,
)
from wxmm.core.errors import CredentialsUnavailable, RateLimited

LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HISTORICAL_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES = ("KXHIGHNY", "HIGHNY")
HIGHNY_NEEDLES = ("HIGHNY", "KXHIGHNY")
# Docs (2026-09-15): authenticated read bucket is token-based; 429 currently has
# no Retry-After. Unauthenticated public GETs are not on that page. Pace anyway.
DEFAULT_MIN_INTERVAL = 0.08
DEFAULT_429_RETRIES = 12


def shard_key(climate: date) -> str:
    return f"{climate.year:04d}-{climate.month:02d}"


def _retry_after_seconds(headers: Any) -> float | None:
    raw = None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return None


def kalshi_auth_headers(url: str, *, api_key: str, api_secret: str) -> dict[str, str]:
    """RSA-PSS headers. Secret is the PEM key from the environment, never a file."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise CredentialsUnavailable(
            "401 received; cryptography is required to sign Kalshi requests"
        ) from exc
    timestamp = str(int(time.time() * 1000))
    path = urlparse(url).path
    message = f"{timestamp}GET{path}".encode("utf-8")
    private_key = serialization.load_pem_private_key(api_secret.encode("utf-8"), password=None)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


class HttpTransport:
    """Injected HTTP for ``TradesTransport``. Historical paths use the external host."""

    def __init__(
        self,
        *,
        live_base: str = LIVE_BASE,
        historical_base: str = HISTORICAL_BASE,
        timeout: float = 45.0,
        max_retries: int = 5,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        raw_store: ContentAddressedRawStore | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.live_base = live_base.rstrip("/")
        self.historical_base = historical_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.n_requests = 0
        self.n_429 = 0
        self.n_401_upgrades = 0
        self.raw_store = raw_store
        self._opener = opener or urllib.request.urlopen
        self._creds: tuple[str, str] | None = None

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _upgrade_auth(self) -> bool:
        if self._creds is not None:
            return False
        from wxmm.live.credentials import load_credentials

        loaded = load_credentials("kalshi", enabled=True)
        self._creds = (loaded.api_key, loaded.api_secret)
        self.n_401_upgrades += 1
        return True

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "wxmm-corpus/1"}
        if self._creds is not None:
            headers.update(
                kalshi_auth_headers(url, api_key=self._creds[0], api_secret=self._creds[1])
            )
        return headers

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self.historical_base if path.startswith("/historical") else self.live_base
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            query = "?" + urllib.parse.urlencode(clean)
        url = f"{base}{path}{query}"
        last_error: Exception | None = None
        attempts = max(self.max_retries, DEFAULT_429_RETRIES)
        for attempt in range(attempts):
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers(url))
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    body_bytes = response.read()
                if self.raw_store is not None:
                    self.raw_store.put(
                        url,
                        body_bytes,
                        meta={"path": path, "ticker": (params or {}).get("ticker")},
                    )
                body = json.loads(body_bytes.decode("utf-8"))
                with self._lock:
                    self.n_requests += 1
                if not isinstance(body, dict):
                    raise ValueError(f"{url} did not return an object")
                return body
            except urllib.error.HTTPError as exc:
                payload = exc.read() if exc.fp is not None else b""
                if exc.code == 404:
                    if self.raw_store is not None:
                        self.raw_store.put(url, payload or b"{}", meta={"http_status": 404})
                    return {"trades": [], "markets": [], "cursor": ""}
                if exc.code == 401:
                    try:
                        if self._upgrade_auth():
                            last_error = exc
                            continue
                    except CredentialsUnavailable:
                        raise
                    raise CredentialsUnavailable(
                        "Kalshi returned 401 and env credentials were not accepted"
                    ) from exc
                if exc.code == 429:
                    with self._lock:
                        self.n_429 += 1
                    retry_after = _retry_after_seconds(exc.headers)
                    delay = retry_after if retry_after is not None else min(2.0**attempt, 30.0)
                    delay += random.uniform(0.0, 0.25)
                    last_error = RateLimited(
                        f"GET {url} 429",
                        retry_after=retry_after,
                    )
                    time.sleep(delay)
                    continue
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last_error = exc
            time.sleep(min(2.0**attempt, 30.0) + random.uniform(0.0, 0.1))
        raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_error}")


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


def pull_one(
    transport: HttpTransport,
    ticker: str,
    cutoff: HistoricalCutoff,
    *,
    limit: int,
    checkpoints: CheckpointStore | None = None,
    complement_tally: ComplementTally | None = None,
) -> tuple[list[RawTrade], dict[str, Any]]:
    historical, live = pull_ticker(
        transport,
        ticker,
        cutoff,
        limit=limit,
        strict_complement=False,
        complement_tally=complement_tally,
        checkpoints=checkpoints,
    )
    merged, report = merge_trades(historical, live, cutoff)
    if complement_tally is not None and complement_tally.violating_ids:
        merged = [trade for trade in merged if trade.trade_id not in complement_tally.violating_ids]
    row = asdict(report)
    row["ticker"] = ticker
    return merged, row


def _merge_existing(
    by_shard: dict[str, list[RawTrade]],
    existing_dir: Path,
) -> dict[str, list[RawTrade]]:
    merged: dict[str, list[RawTrade]] = {}
    for shard, trades in by_shard.items():
        path = existing_dir / f"climate_month={shard}.parquet"
        prior = read_trades_parquet(path) if path.exists() else []
        seen: dict[str, RawTrade] = {t.trade_id: t for t in prior}
        for trade in trades:
            seen[trade.trade_id] = trade
        merged[shard] = list(seen.values())
    return merged


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


def compact_ticker_parquets(ticker_dir: Path, out_dir: Path) -> dict[str, int]:
    if not ticker_dir.is_dir():
        return {}
    files = sorted(ticker_dir.glob("*.parquet"))
    if not files:
        return {}
    import polars as pl

    frame = pl.concat([pl.read_parquet(path) for path in files], how="vertical")
    if "climate_day" not in frame.columns or frame.is_empty():
        return {}
    frame = frame.unique(subset=["trade_id"], keep="last").sort(["created_time", "trade_id"])
    frame = frame.with_columns(
        pl.col("climate_day").cast(pl.Utf8).str.slice(0, 7).alias("_month")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for shard in frame.get_column("_month").unique().sort().to_list():
        part = frame.filter(pl.col("_month") == shard).drop("_month")
        dest = out_dir / f"climate_month={shard}.parquet"
        part.write_parquet(dest)
        counts[str(shard)] = part.height
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
    only_tickers: Sequence[str] = (),
    merge_into: Path | None = None,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_store = ContentAddressedRawStore(out_dir / "raw")
    checkpoints = CheckpointStore(out_dir / "_checkpoints")
    ticker_dir = out_dir / "_tickers"
    ticker_dir.mkdir(parents=True, exist_ok=True)
    transport = HttpTransport(min_interval=min_interval, raw_store=raw_store)
    cutoff = fetch_cutoff(transport)
    cutoff_dt = datetime.fromtimestamp(cutoff.trade_partition_ts, tz=timezone.utc)
    print(
        f"cutoff market_settled_ts={cutoff.market_settled_ts} "
        f"trades_created_ts={cutoff.trades_created_ts} ({cutoff_dt.isoformat()})"
    )

    scoped: dict[str, date] = {}
    all_markets: list[dict[str, Any]] = []
    for name in series:
        markets = enumerate_markets(transport, name)
        print(f"series {name}: {len(markets)} markets in archive")
        all_markets.extend(markets)
        for market in markets:
            ticker = str(market.get("ticker") or "")
            climate = in_scope(ticker, start=start, end=end)
            if climate is not None:
                scoped[ticker] = climate
    (out_dir / "markets.json").write_text(
        json.dumps(all_markets), encoding="utf-8"
    )
    if only_tickers:
        wanted = set(only_tickers)
        missing = wanted - set(scoped)
        if missing:
            print(f"not in scope, skipped: {sorted(missing)}", file=sys.stderr)
        scoped = {t: d for t, d in scoped.items() if t in wanted}
    done = checkpoints.done_tickers()
    for path in ticker_dir.glob("*.parquet"):
        done.add(path.stem)
    tickers = sorted(t for t in scoped if t not in done)
    print(f"in scope {start} .. {end}: {len(scoped)} tickers, {len(tickers)} remaining")
    if not scoped:
        print("nothing to pull", file=sys.stderr)
        return 1

    reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    complement = ComplementTally()
    lock = threading.Lock()
    finished = 0
    started = time.monotonic()
    n_boundary = 0
    n_duplicate = 0
    gap_notes: list[str] = []

    def work(ticker: str) -> None:
        nonlocal finished, n_boundary, n_duplicate
        local_tally = ComplementTally()
        try:
            merged, report = pull_one(
                transport,
                ticker,
                cutoff,
                limit=limit,
                checkpoints=checkpoints,
                complement_tally=local_tally,
            )
        except (RuntimeError, ValueError, KeyError, CredentialsUnavailable) as exc:
            with lock:
                failures.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
                finished += 1
            return
        dest = checkpoints.ticker_parquet(ticker_dir, ticker)
        if merged:
            trades_to_parquet(merged, dest)
        checkpoints.mark_done(ticker)
        inflight_h = checkpoints.inflight_path(ticker, "historical")
        inflight_l = checkpoints.inflight_path(ticker, "live")
        if inflight_h.exists():
            inflight_h.unlink()
        if inflight_l.exists():
            inflight_l.unlink()
        with lock:
            complement.merge_from(local_tally)
            reports.append(report)
            n_boundary += int(report["n_boundary_overlap"])
            n_duplicate += int(report["n_duplicate_ids"])
            if report["gap_note"]:
                gap_notes.append(str(report["gap_note"]))
            finished += 1
            if progress_every and finished % progress_every == 0:
                rate = finished / max(time.monotonic() - started, 1e-9)
                print(
                    f"  {finished}/{len(tickers)} remaining-tickers  "
                    f"{rate:.1f} tick/s  {len(failures)} failed  "
                    f"complement_violations={complement.n_violations}",
                    flush=True,
                )

    if tickers:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, tickers))

    if merge_into is not None:
        leftover: dict[str, list[RawTrade]] = defaultdict(list)
        for ticker in only_tickers:
            path = checkpoints.ticker_parquet(ticker_dir, ticker)
            if path.exists():
                leftover[shard_key(scoped[ticker])].extend(
                    read_trades_parquet(path, strict_complement=False)
                )
        write_shards(_merge_existing(leftover, merge_into), merge_into)

    counts = compact_ticker_parquets(ticker_dir, out_dir)
    total = sum(counts.values())
    elapsed = time.monotonic() - started
    raw_bytes = 0
    if raw_store.index_path.exists():
        raw_bytes = sum(
            int(json.loads(line).get("nbytes") or 0)
            for line in raw_store.index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    meta = {
        "pulled_at": datetime.now(tz=timezone.utc).isoformat(),
        "series": list(series),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cutoff_market_settled_ts": cutoff.market_settled_ts,
        "cutoff_trades_created_ts": cutoff.trades_created_ts,
        "cutoff_iso": cutoff_dt.isoformat(),
        "n_tickers_scoped": len(scoped),
        "n_tickers_with_trades": len(reports) + len(done),
        "n_failures": len(failures),
        "failures": failures[:200],
        "n_trades": total,
        "shards": counts,
        "n_requests": transport.n_requests,
        "n_429": transport.n_429,
        "n_401_upgrades": transport.n_401_upgrades,
        "n_duplicate_ids": n_duplicate,
        "n_boundary_overlap": n_boundary,
        "gap_notes": gap_notes[:50],
        "complement": complement.as_dict(),
        "wall_clock_s": round(elapsed, 3),
        "raw_store_bytes": raw_bytes,
    }
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
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL)
    parser.add_argument("--progress-every", type=int, default=250)
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=[],
        help="Pull only these tickers (targeted retry of failures)",
    )
    parser.add_argument(
        "--merge-into",
        type=Path,
        default=None,
        help="Fold results into existing shards in this directory",
    )
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
        only_tickers=args.tickers,
        merge_into=args.merge_into,
    )


if __name__ == "__main__":
    raise SystemExit(main())
