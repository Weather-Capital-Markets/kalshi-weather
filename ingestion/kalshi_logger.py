"""Kalshi KXHIGHNY market-data logger."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ingestion.client import AuthStub, KalshiClient, RequestResult
from ingestion.config_loader import load_config
from ingestion.heartbeat import (
    connect,
    format_status_summary,
    init_schema,
    last_hour_summary,
    record_attempt,
)
from ingestion.state import (
    clear_state,
    extract_trade_ids,
    filter_new_trade_ids,
    get_open_tickers,
    get_state,
    init_state_schema,
    list_pending_settlement,
    mark_settlement_captured,
    mark_trades_seen,
    queue_dropped_markets,
    set_state,
    trade_cursor_key,
    upsert_known_markets,
)
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)


@dataclass
class Scheduler:
    cadence_sec: dict[str, float]
    last_run: dict[str, float] = field(default_factory=dict)

    def due(self, name: str, now: float | None = None) -> bool:
        current = now if now is not None else time.monotonic()
        last = self.last_run.get(name)
        if last is None:
            return True
        return (current - last) >= self.cadence_sec[name]

    def mark(self, name: str, now: float | None = None) -> None:
        self.last_run[name] = now if now is not None else time.monotonic()


class KalshiLogger:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["heartbeat_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        self.client = KalshiClient(config, auth=self._load_auth())
        self.series = list(config.get("series", []))
        self.scheduler = Scheduler({k: float(v) for k, v in config["cadence_sec"].items()})
        self._shutdown = False
        self._active_tickers: set[str] = set()

    def _load_auth(self) -> AuthStub:
        return AuthStub(
            os.environ.get("KALSHI_API_KEY_ID"),
            os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
        )

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

    def run_forever(self) -> None:
        self.install_signal_handlers()
        logger.info("starting kalshi logger for series=%s", self.series)
        while not self._shutdown:
            self.run_cycle()
            time.sleep(1)
        logger.info("shutdown complete")

    def run_once(self) -> None:
        for task in ("markets", "orderbook", "trades", "settlement"):
            self.scheduler.last_run.pop(task, None)
        self.run_cycle(force_all=True)

    def run_cycle(self, *, force_all: bool = False) -> None:
        now = time.monotonic()
        if force_all or self.scheduler.due("markets", now):
            self.poll_markets()
            self.scheduler.mark("markets")
        if force_all or self.scheduler.due("orderbook", now):
            self.poll_orderbooks()
            self.scheduler.mark("orderbook")
        if force_all or self.scheduler.due("trades", now):
            self.poll_trades()
            self.scheduler.mark("trades")
        if force_all or self.scheduler.due("settlement", now):
            self.poll_settlement()
            self.scheduler.mark("settlement")

    def poll_markets(self) -> None:
        for series_ticker in self.series:
            self._poll_markets_for_series(series_ticker)

    def _poll_markets_for_series(self, series_ticker: str) -> None:
        path = self.client.path("markets")
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 1000,
        }
        cursor: str | None = None
        all_markets: list[dict[str, Any]] = []
        discovery_complete = False

        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            result = self.client.get(path, params=page_params)
            self._record(result, ticker=series_ticker)
            payload = result.json_body if isinstance(result.json_body, dict) else None
            if result.ok and payload:
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=path,
                    category="markets",
                    key=series_ticker,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=payload,
                )
                markets = payload.get("markets")
                if isinstance(markets, list):
                    all_markets.extend(m for m in markets if isinstance(m, dict))
                cursor = payload.get("cursor") or ""
                if not cursor:
                    discovery_complete = True
                    break
            else:
                break

        if discovery_complete:
            ts_utc = utc_now_iso()
            tickers = [str(m["ticker"]) for m in all_markets if m.get("ticker") is not None]
            previously_open = get_open_tickers(self.conn) or self._active_tickers
            upsert_known_markets(self.conn, tickers=tickers, ts_utc=ts_utc, status="open")
            queue_dropped_markets(
                self.conn,
                previously_open=previously_open,
                currently_open=set(tickers),
                ts_utc=ts_utc,
            )
            self._active_tickers = set(tickers)
        elif not self._active_tickers:
            self._active_tickers = get_open_tickers(self.conn)

    def poll_orderbooks(self) -> None:
        tickers = sorted(self._active_tickers or get_open_tickers(self.conn))
        for ticker in tickers:
            path = self.client.path("orderbook", ticker=ticker)
            params = {"depth": self.client.orderbook_depth}
            result = self.client.get(path, params=params)
            self._record(result, ticker=ticker)
            if result.ok:
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=path,
                    category="orderbook",
                    key=ticker,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=result.json_body,
                )

    def poll_trades(self) -> None:
        for series_ticker in self.series:
            self._poll_trades_for_series(series_ticker)

    def _poll_trades_for_series(self, series_ticker: str) -> None:
        tickers = sorted(self._active_tickers or get_open_tickers(self.conn))
        for ticker in tickers:
            self._poll_trades_for_market(series_ticker, ticker)

    def _poll_trades_for_market(self, series_ticker: str, ticker: str) -> None:
        path = self.client.path("trades")
        cursor_key = trade_cursor_key(ticker)
        cursor = get_state(self.conn, cursor_key) or ""

        while not self._shutdown:
            params: dict[str, Any] = {
                "limit": self.client.trades_page_limit,
                "ticker": ticker,
            }
            if cursor:
                params["cursor"] = cursor

            result = self.client.get(path, params=params)
            self._record(result, ticker=ticker)
            if not result.ok or not isinstance(result.json_body, dict):
                return

            payload = result.json_body
            trade_ids = extract_trade_ids(payload)
            new_ids = filter_new_trade_ids(self.conn, trade_ids)
            next_cursor = payload.get("cursor") or ""
            reached_seen_page = bool(trade_ids) and not new_ids
            if not next_cursor or reached_seen_page:
                if not reached_seen_page:
                    ts_utc = self._persist_trades_page(
                        series_ticker=series_ticker,
                        path=path,
                        result=result,
                        payload=payload,
                    )
                clear_state(self.conn, cursor_key)
                if not reached_seen_page and new_ids:
                    mark_trades_seen(self.conn, new_ids, ts_utc)
                return

            ts_utc = self._persist_trades_page(
                series_ticker=series_ticker,
                path=path,
                result=result,
                payload=payload,
            )
            # The raw page is durable before the cursor advances. Seen IDs follow;
            # a crash between those commits can duplicate data but cannot lose it.
            set_state(self.conn, cursor_key, str(next_cursor))
            if new_ids:
                mark_trades_seen(self.conn, new_ids, ts_utc)
            cursor = str(next_cursor)

    def _persist_trades_page(
        self,
        *,
        series_ticker: str,
        path: str,
        result: RequestResult,
        payload: dict[str, Any],
    ) -> str:
        ts_utc = utc_now_iso()
        self.writer.write(
            ts_utc=ts_utc,
            endpoint=path,
            category="trades",
            key=series_ticker,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            payload=payload,
        )
        return ts_utc

    def poll_settlement(self) -> None:
        pending = list_pending_settlement(self.conn)
        for ticker in pending:
            path = self.client.path("market", ticker=ticker)
            result = self.client.get(path)
            self._record(result, ticker=ticker)
            if result.ok and isinstance(result.json_body, dict):
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=path,
                    category="markets",
                    key=ticker,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=result.json_body,
                )
                mark_settlement_captured(self.conn, ticker)

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


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="ts_utc=%(asctime)sZ level=%(levelname)s logger=%(name)s message=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=[handler],
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi market-data logger")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ingestion/config.yaml)",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print last-hour heartbeat summary and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    configure_logging(config.get("logging", {}).get("level", "INFO"))

    if args.status:
        conn = connect(config["storage"]["heartbeat_db"])
        init_schema(conn)
        print(format_status_summary(last_hour_summary(conn)))
        conn.close()
        return 0

    app = KalshiLogger(config)
    try:
        if args.once:
            app.run_once()
        else:
            app.run_forever()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
