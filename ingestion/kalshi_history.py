"""Laptop-only Kalshi settled-market and candlestick backfill.

Allowed DB: storage.backfill_db only. Never open storage.heartbeat_db.
Do not run on the VPS — that request budget belongs to the live logger.

Probe before dry-run before bulk. See README.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingestion.client import KalshiClient, RequestResult
from ingestion.config_loader import load_config
from ingestion.heartbeat import connect, init_schema, record_attempt
from ingestion.state import (
    get_candle_progress,
    init_backfill_schema,
    init_state_schema,
    list_history_markets,
    set_candle_progress,
    upsert_history_market,
)
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)

BYTES_PER_CANDLE_EST = 500
RATE_NOTE_GB = 20
RATE_NOTE_HOURS = 24


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _market_dt(market: dict[str, Any], key: str) -> datetime | None:
    raw = market.get(key)
    return _parse_iso(raw if isinstance(raw, str) else None)


def _unix(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.timestamp())


def cutoff_ts(payload: dict[str, Any] | None) -> int | None:
    """Extract market_settled_ts. Shape is # TODO(verify) against --probe output."""
    if not payload:
        return None
    for key in ("market_settled_ts", "marketSettledTs"):
        raw = payload.get(key)
        if raw is not None:
            break
    else:
        nested = payload.get("cutoff") or payload.get("cutoffs") or {}
        raw = nested.get("market_settled_ts") if isinstance(nested, dict) else None
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    dt = _parse_iso(str(raw))
    return _unix(dt)


class HistoryBackfill:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["backfill_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        init_backfill_schema(self.conn)
        self.client = KalshiClient(config)
        hist = config.get("history", {})
        self.series = list(hist.get("series") or config.get("series") or [])
        self.statuses = list(hist.get("statuses") or ["settled", "closed"])
        self.period = int(hist.get("candle_period_minutes", 1))
        self.chunk_sec = int(hist.get("candle_chunk_minutes", 1440)) * 60
        self._shutdown = False

    def close(self) -> None:
        self.writer.close()
        self.client.close()
        self.conn.close()

    def install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: Any) -> None:
            logger.info("received signal %s, shutting down", signum)
            self._shutdown = True

        signal.signal(signal.SIGTERM, _handle)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, _handle)

    def _record(self, result: RequestResult, *, ticker: str) -> None:
        attempts = result.attempts or (result,)
        for attempt in attempts:
            record_attempt(
                self.conn,
                ts_utc=utc_now_iso(),
                endpoint=result.endpoint,
                ticker=ticker,
                ok=attempt.ok,
                http_status=attempt.status_code,
                latency_ms=attempt.latency_ms,
                error_text=attempt.error_text,
            )

    def fetch_cutoff(self) -> tuple[RequestResult, int | None]:
        path = self.client.path("historical_cutoff")
        result = self.client.get(path)
        self._record(result, ticker="cutoff")
        body = result.json_body if isinstance(result.json_body, dict) else None
        return result, cutoff_ts(body)

    def _paginate_markets(
        self,
        *,
        path: str,
        params: dict[str, Any],
        series_ticker: str,
        category_key: str,
        persist: bool,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        collected: list[dict[str, Any]] = []
        pages = 0
        while not self._shutdown:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            result = self.client.get(path, params=page_params)
            self._record(result, ticker=series_ticker)
            payload = result.json_body if isinstance(result.json_body, dict) else None
            if not result.ok or payload is None:
                logger.warning(
                    "markets page failed series=%s status=%s",
                    series_ticker,
                    result.status_code,
                )
                break
            if persist:
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=path,
                    category="markets_history",
                    key=category_key,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=payload,
                )
            markets = payload.get("markets")
            if isinstance(markets, list):
                collected.extend(m for m in markets if isinstance(m, dict))
            pages += 1
            cursor = payload.get("cursor") or ""
            if not cursor:
                break
            if max_pages is not None and pages >= max_pages:
                break
        return collected

    def enumerate_markets(self, *, persist: bool) -> list[dict[str, Any]]:
        _cutoff_result, settled_cutoff = self.fetch_cutoff()
        logger.info("historical cutoff market_settled_ts=%s", settled_cutoff)
        all_markets: dict[str, dict[str, Any]] = {}
        live_path = self.client.path("markets")
        hist_path = self.client.path("historical_markets")

        for series_ticker in self.series:
            for status in self.statuses:
                live = self._paginate_markets(
                    path=live_path,
                    params={
                        "series_ticker": series_ticker,
                        "status": status,
                        "limit": 1000,
                    },
                    series_ticker=series_ticker,
                    category_key=f"{series_ticker}_{status}",
                    persist=persist,
                )
                for market in live:
                    ticker = str(market.get("ticker") or "")
                    if ticker:
                        market["_series_ticker"] = series_ticker
                        all_markets[ticker] = market
            historical = self._paginate_markets(
                path=hist_path,
                params={"series_ticker": series_ticker, "limit": 1000},
                series_ticker=series_ticker,
                category_key=f"{series_ticker}_historical",
                persist=persist,
            )
            for market in historical:
                ticker = str(market.get("ticker") or "")
                if ticker:
                    market["_series_ticker"] = series_ticker
                    all_markets.setdefault(ticker, market)
        return list(all_markets.values())

    def persist_market_index(self, markets: list[dict[str, Any]]) -> None:
        ts_utc = utc_now_iso()
        for market in markets:
            ticker = str(market.get("ticker") or "")
            if not ticker:
                continue
            upsert_history_market(
                self.conn,
                ticker=ticker,
                series_ticker=str(market.get("_series_ticker") or ""),
                open_time=str(market.get("open_time") or "") or None,
                close_time=str(market.get("close_time") or "") or None,
                status=str(market.get("status") or "") or None,
                enumerated_utc=ts_utc,
            )

    def print_dry_run(self, markets: list[dict[str, Any]]) -> None:
        hours: list[float] = []
        opens: list[datetime] = []
        closes: list[datetime] = []
        by_series: dict[str, int] = {}
        for market in markets:
            series = str(market.get("_series_ticker") or "?")
            by_series[series] = by_series.get(series, 0) + 1
            open_dt = _market_dt(market, "open_time")
            close_dt = _market_dt(market, "close_time")
            if open_dt:
                opens.append(open_dt)
            if close_dt:
                closes.append(close_dt)
            if open_dt and close_dt and close_dt > open_dt:
                hours.append((close_dt - open_dt).total_seconds() / 3600.0)
        n = len(markets)
        mean_h = sum(hours) / len(hours) if hours else 0.0
        candles = n * mean_h * 60.0
        bytes_est = candles * BYTES_PER_CANDLE_EST
        gb_est = bytes_est / (1024**3)
        reqs = 0.0
        if self.chunk_sec > 0:
            reqs = (
                sum(max(1.0, (h * 3600.0) / self.chunk_sec) for h in hours) if hours else float(n)
            )
        rps = float(self.config["api"].get("max_requests_per_sec") or 5)
        fetch_hours = (reqs / rps) / 3600.0 if rps else float("inf")
        span = "n/a"
        if opens and closes:
            span = f"{min(opens).date().isoformat()} → {max(closes).date().isoformat()}"
        print("dry-run: market enumeration only (no candlesticks fetched)")
        print(f"markets: {n}")
        print(f"by_series: {by_series}")
        print(f"date_span_open_close: {span}")
        print(f"mean_open_hours: {mean_h:.2f}")
        print(
            "volume_estimate: "
            f"n_markets * mean_open_hours * 60 * {BYTES_PER_CANDLE_EST} B "
            f"= {gb_est:.2f} GB, ~{int(candles):,} one-minute candles"
        )
        print(f"fetch_time_estimate_at_{rps:g}_rps: {fetch_hours:.2f} h ({int(reqs):,} requests)")
        print(
            "contingency: if estimate exceeds "
            f"~{RATE_NOTE_GB} GB or ~{RATE_NOTE_HOURS} h, restrict candles to "
            "[T-72h, close] per market (census horizons only reach T-48h). "
            "That decision is yours at this readout — this process will not apply it."
        )
        if gb_est > RATE_NOTE_GB or fetch_hours > RATE_NOTE_HOURS:
            print("NOTE: estimate exceeds the contingency threshold.")

    def _use_historical(self, market: dict[str, Any], settled_cutoff: int | None) -> bool:
        settle = _market_dt(market, "settlement_ts")
        close = _market_dt(market, "close_time")
        ts = _unix(settle) or _unix(close)
        if settled_cutoff is None or ts is None:
            return False
        return ts < settled_cutoff

    def _fetch_window(
        self,
        *,
        market: dict[str, Any],
        start_ts: int,
        end_ts: int,
        settled_cutoff: int | None,
    ) -> RequestResult:
        ticker = str(market["ticker"])
        series = str(market.get("_series_ticker") or "")
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": self.period,
        }
        if self._use_historical(market, settled_cutoff):
            path = self.client.path("historical_candlesticks", ticker=ticker)
        else:
            path = self.client.path("candlesticks", series_ticker=series, ticker=ticker)
        result = self.client.get(path, params=params)
        self._record(result, ticker=ticker)
        return result

    def probe(self, ticker: str | None) -> int:
        cutoff_result, settled_cutoff = self.fetch_cutoff()
        print("=== PROBE raw /historical/cutoff ===")
        print(json.dumps(cutoff_result.json_body, indent=2, default=str))
        live_path = self.client.path("markets")
        page = self._paginate_markets(
            path=live_path,
            params={"series_ticker": self.series[0], "status": "settled", "limit": 10},
            series_ticker=self.series[0],
            category_key="probe",
            persist=False,
            max_pages=1,
        )
        if not page:
            hist_path = self.client.path("historical_markets")
            page = self._paginate_markets(
                path=hist_path,
                params={"series_ticker": self.series[0], "limit": 10},
                series_ticker=self.series[0],
                category_key="probe",
                persist=False,
                max_pages=1,
            )
        market = None
        if ticker:
            for item in page:
                if item.get("ticker") == ticker:
                    market = item
                    break
            if market is None:
                path = self.client.path("market", ticker=ticker)
                result = self.client.get(path)
                self._record(result, ticker=ticker)
                body = result.json_body if isinstance(result.json_body, dict) else None
                print("=== PROBE raw GET /markets/{ticker} ===")
                print(json.dumps(body, indent=2, default=str))
                if body and isinstance(body.get("market"), dict):
                    market = body["market"]
                    market["_series_ticker"] = self.series[0]
        if market is None and page:
            market = page[0]
            market["_series_ticker"] = market.get("_series_ticker") or self.series[0]
        if market is None:
            print("probe: no market found")
            return 1
        print("=== PROBE raw market object ===")
        print(json.dumps(market, indent=2, default=str))
        open_ts = _unix(_market_dt(market, "open_time"))
        close_ts = _unix(_market_dt(market, "close_time"))
        now_ts = int(time.time())
        end_ts = close_ts or now_ts
        start_ts = open_ts or (end_ts - self.chunk_sec)
        window_end = min(end_ts, start_ts + self.chunk_sec)
        result = self._fetch_window(
            market=market,
            start_ts=start_ts,
            end_ts=window_end,
            settled_cutoff=settled_cutoff,
        )
        print(
            "=== PROBE raw candlesticks "
            f"endpoint={result.endpoint} status={result.status_code} ==="
        )
        print(json.dumps(result.json_body, indent=2, default=str))
        return 0 if result.ok else 1

    def backfill_candles(self) -> None:
        _cutoff_result, settled_cutoff = self.fetch_cutoff()
        rows = list_history_markets(self.conn)
        for row in rows:
            if self._shutdown:
                break
            ticker = row["ticker"]
            progress = get_candle_progress(self.conn, ticker)
            if progress and progress["complete"]:
                continue
            market = {
                "ticker": ticker,
                "_series_ticker": row["series_ticker"],
                "open_time": row["open_time"],
                "close_time": row["close_time"],
                "status": row["status"],
            }
            open_ts = _unix(_parse_iso(row["open_time"]))
            close_ts = _unix(_parse_iso(row["close_time"])) or int(time.time())
            if open_ts is None:
                logger.warning("skip %s: missing open_time", ticker)
                continue
            cursor_ts = int(progress["last_end_ts"]) if progress else open_ts
            while cursor_ts < close_ts and not self._shutdown:
                window_end = min(close_ts, cursor_ts + self.chunk_sec)
                result = self._fetch_window(
                    market=market,
                    start_ts=cursor_ts,
                    end_ts=window_end,
                    settled_cutoff=settled_cutoff,
                )
                if not result.ok:
                    logger.warning(
                        "candles failed ticker=%s status=%s",
                        ticker,
                        result.status_code,
                    )
                    break
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=result.endpoint,
                    category="candlesticks",
                    key=ticker,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=result.json_body,
                )
                complete = window_end >= close_ts
                set_candle_progress(
                    self.conn,
                    ticker=ticker,
                    last_end_ts=window_end,
                    complete=complete,
                    updated_utc=utc_now_iso(),
                )
                cursor_ts = window_end


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="ts_utc=%(asctime)sZ level=%(levelname)s logger=%(name)s message=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi historical market backfill")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Fetch cutoff + one candlestick page; print raw JSON; do not bulk-run",
    )
    parser.add_argument("--ticker", default=None, help="Market ticker for --probe")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate markets, print volume estimate, stop",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.probe and args.dry_run:
        parser.error("--probe and --dry-run are sequential checkpoints; run probe first")
    config = load_config(args.config)
    configure_logging(config.get("logging", {}).get("level", "INFO"))
    app = HistoryBackfill(config)
    app.install_signal_handlers()
    try:
        if args.probe:
            return app.probe(args.ticker)
        markets = app.enumerate_markets(persist=not args.dry_run)
        if args.dry_run:
            app.print_dry_run(markets)
            return 0
        app.persist_market_index(markets)
        app.backfill_candles()
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
