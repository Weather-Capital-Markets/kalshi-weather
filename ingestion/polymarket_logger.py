"""Polymarket US public market-data logger.

Allowed DB: polymarket storage.heartbeat_db only. Isolated from Kalshi logger.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ingestion.config_loader import load_config
from ingestion.heartbeat import (
    connect,
    format_status_summary,
    init_schema,
    last_hour_summary,
    record_attempt,
)
from ingestion.polymarket_client import PolymarketClient
from ingestion.state import get_state, init_state_schema, set_state
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)

ACTIVE_SLUGS_KEY = "pm_active_slugs"


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


def _extract_markets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        markets = payload.get("markets")
        if isinstance(markets, list):
            return [m for m in markets if isinstance(m, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return []


def _market_slug(market: dict[str, Any]) -> str | None:
    for key in ("slug", "marketSlug", "market_slug"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class PolymarketLogger:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["heartbeat_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        self.client = PolymarketClient(config)
        self.markets_cfg = config.get("markets") or {}
        self.scheduler = Scheduler({k: float(v) for k, v in config["cadence_sec"].items()})
        self._shutdown = False
        self._active_slugs: set[str] = set()

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
        logger.info("starting polymarket logger")
        while not self._shutdown:
            self.run_cycle()
            time.sleep(1)
        logger.info("shutdown complete")

    def run_once(self) -> None:
        for task in ("markets", "orderbook"):
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

    def poll_markets(self) -> None:
        path = str(self.markets_cfg.get("list_path") or "/v1/markets")
        params: dict[str, Any] = {"limit": int(self.markets_cfg.get("limit") or 100)}
        slug_filter = str(self.markets_cfg.get("slug_filter") or "").strip()
        if slug_filter:
            params["search"] = slug_filter
        result = self.client.get(path, params=params)
        self._record(result, ticker="markets")
        if result.ok and isinstance(result.json_body, (dict, list)):
            payload = result.json_body
            if not isinstance(payload, dict):
                payload = {"markets": result.json_body}
            self.writer.write(
                ts_utc=utc_now_iso(),
                endpoint=path,
                category="pm_markets",
                key="list",
                http_status=result.status_code,
                latency_ms=result.latency_ms,
                payload=payload,
            )
            slugs = []
            for market in _extract_markets(result.json_body):
                slug = _market_slug(market)
                if slug:
                    slugs.append(slug)
            configured = [
                str(s).strip() for s in (self.markets_cfg.get("slugs") or []) if str(s).strip()
            ]
            if configured:
                slugs = configured
            self._active_slugs = set(slugs)
            set_state(self.conn, ACTIVE_SLUGS_KEY, json.dumps(sorted(self._active_slugs)))
        elif not self._active_slugs:
            raw = get_state(self.conn, ACTIVE_SLUGS_KEY)
            if raw:
                try:
                    self._active_slugs = set(json.loads(raw))
                except json.JSONDecodeError:
                    self._active_slugs = set()

    def poll_orderbooks(self) -> None:
        if not self._active_slugs:
            raw = get_state(self.conn, ACTIVE_SLUGS_KEY)
            if raw:
                try:
                    self._active_slugs = set(json.loads(raw))
                except json.JSONDecodeError:
                    self._active_slugs = set()
        book_template = str(self.markets_cfg.get("book_path_template") or "/v1/markets/{slug}/book")
        for slug in sorted(self._active_slugs):
            path = book_template.replace("{slug}", slug)
            result = self.client.get(path)
            self._record(result, ticker=slug)
            if result.ok and isinstance(result.json_body, dict):
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=path,
                    category="pm_orderbook",
                    key=slug,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=result.json_body,
                )

    def probe(self) -> int:
        path = str(self.markets_cfg.get("list_path") or "/v1/markets")
        params: dict[str, Any] = {"limit": int(self.markets_cfg.get("limit") or 10)}
        slug_filter = str(self.markets_cfg.get("slug_filter") or "").strip()
        if slug_filter:
            params["search"] = slug_filter
        result = self.client.get(path, params=params)
        print("=== PROBE GET markets list ===")
        print(f"status={result.status_code}")
        print(json.dumps(result.json_body, indent=2, default=str))
        markets = _extract_markets(result.json_body)
        slug = None
        configured = [
            str(s).strip() for s in (self.markets_cfg.get("slugs") or []) if str(s).strip()
        ]
        if configured:
            slug = configured[0]
        elif markets:
            slug = _market_slug(markets[0])
        if not slug:
            print("probe: no market slug resolved — set polymarket.markets.slugs in config")
            return 0
        book_template = str(self.markets_cfg.get("book_path_template") or "/v1/markets/{slug}/book")
        book_path = book_template.replace("{slug}", slug)
        book_result = self.client.get(book_path)
        print(f"=== PROBE GET {book_path} ===")
        print(f"status={book_result.status_code}")
        print(json.dumps(book_result.json_body, indent=2, default=str))
        return 0

    def _record(self, result: Any, *, ticker: str) -> None:
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
    logging.basicConfig(level=level, handlers=[handler], force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket US market-data logger")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Fetch one market list + book raw JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    pm_config = config.get("polymarket") or {}
    if not pm_config:
        print("polymarket block missing in config")
        return 1
    configure_logging(config.get("logging", {}).get("level", "INFO"))

    if args.status:
        conn = connect(pm_config["storage"]["heartbeat_db"])
        init_schema(conn)
        print(format_status_summary(last_hour_summary(conn)))
        conn.close()
        return 0

    app = PolymarketLogger(pm_config)
    try:
        if args.probe:
            return app.probe()
        if args.once:
            app.run_once()
        else:
            app.run_forever()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
