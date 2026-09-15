"""Polymarket NYC daily-high temperature logger (Gamma discovery + CLOB books).

Allowed DB: polymarket storage.heartbeat_db only. Isolated from Kalshi logger.

Deferred analysis (logging only — not implemented here):
  (a) S2-on-Polymarket = bracket probability sum law (Σ Yes across negRisk set ≈ 1).
  (b) cross-venue = bracket-to-bracket comparison (KLGA vs KNYC basis — see venue-facts §3).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from ingestion.polymarket_clob import PolymarketClobClient
from ingestion.polymarket_gamma import (
    PolymarketGammaClient,
    bracket_ladder_set,
    discover_daily_events,
    open_bracket_markets,
    resolve_event_for_day,
)
from ingestion.state import get_state, init_state_schema, set_state
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)

ACTIVE_LADDER_KEY = "pm_active_ladder"


@dataclass
class LadderEntry:
    event_slug: str
    market_slug: str
    yes_token_id: str
    bracket_kind: str
    bracket_low_f: int | None
    bracket_high_f: int | None
    bracket_label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_slug": self.event_slug,
            "market_slug": self.market_slug,
            "yes_token_id": self.yes_token_id,
            "bracket_kind": self.bracket_kind,
            "bracket_low_f": self.bracket_low_f,
            "bracket_high_f": self.bracket_high_f,
            "bracket_label": self.bracket_label,
        }


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


class PolymarketLogger:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["heartbeat_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        self.gamma = PolymarketGammaClient(config)
        self.clob = PolymarketClobClient(config)
        self.markets_cfg = config.get("markets") or {}
        self.gamma_cfg = config.get("gamma") or {}
        self.scheduler = Scheduler({k: float(v) for k, v in config["cadence_sec"].items()})
        self._shutdown = False
        self._active_ladder: list[LadderEntry] = []

    def close(self) -> None:
        self.writer.close()
        self.gamma.close()
        self.clob.close()
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

    def _series_slug(self) -> str:
        return str(
            self.markets_cfg.get("series_slug")
            or self.gamma_cfg.get("series_slug")
            or "nyc-daily-weather"
        )

    def _event_prefix(self) -> str:
        return str(self.gamma_cfg.get("event_slug_prefix") or "highest-temperature-in-nyc-on")

    def _horizon_days(self) -> int:
        return int(self.markets_cfg.get("horizon_days") or self.gamma_cfg.get("horizon_days") or 2)

    def poll_markets(self) -> None:
        series_slug = self._series_slug()
        discovered, gamma_results = discover_daily_events(
            self.gamma,
            series_slug=series_slug,
            event_prefix=self._event_prefix(),
            horizon_days=self._horizon_days(),
            discovery_limit=int(self.gamma_cfg.get("discovery_limit") or 10),
        )
        for result in gamma_results:
            ticker = "gamma_series" if result.endpoint == "/events" else "gamma_event"
            self._record(result, ticker=ticker)

        ladder_entries: list[LadderEntry] = []
        for event_slug, event in discovered:
            markets = open_bracket_markets(event)
            ladder = bracket_ladder_set(markets)
            endpoint = f"/events/slug/{event_slug}"
            self.writer.write(
                ts_utc=utc_now_iso(),
                endpoint=endpoint,
                category="pm_markets",
                key=event_slug,
                http_status=200,
                latency_ms=0,
                payload={
                    "event_slug": event_slug,
                    "series_slug": series_slug,
                    "event": event,
                    "markets": markets,
                    "ladder": ladder,
                },
            )
            for market in markets:
                meta = market.get("pm_meta") if isinstance(market.get("pm_meta"), dict) else {}
                token = meta.get("yes_token_id")
                slug = meta.get("market_slug")
                kind = meta.get("bracket_kind")
                label = meta.get("bracket_label")
                if (
                    isinstance(token, str)
                    and token
                    and isinstance(slug, str)
                    and isinstance(kind, str)
                    and isinstance(label, str)
                ):
                    low = meta.get("bracket_low_f")
                    high = meta.get("bracket_high_f")
                    ladder_entries.append(
                        LadderEntry(
                            event_slug=event_slug,
                            market_slug=slug,
                            yes_token_id=token,
                            bracket_kind=kind,
                            bracket_low_f=low if isinstance(low, int) else None,
                            bracket_high_f=high if isinstance(high, int) else None,
                            bracket_label=label,
                        )
                    )

        if ladder_entries:
            self._active_ladder = ladder_entries
            set_state(
                self.conn,
                ACTIVE_LADDER_KEY,
                json.dumps([entry.as_dict() for entry in ladder_entries]),
            )
        elif not self._active_ladder:
            self._restore_ladder_state()

    def poll_orderbooks(self) -> None:
        if not self._active_ladder:
            self._restore_ladder_state()
        book_path = str((self.config.get("clob") or {}).get("book_path") or "/book")
        for entry in self._active_ladder:
            result = self.clob.get_book(entry.yes_token_id)
            self._record(result, ticker=entry.market_slug)
            payload: dict[str, Any] = {
                "pm_meta": entry.as_dict(),
                "book": result.json_body,
            }
            if result.ok:
                self.writer.write(
                    ts_utc=utc_now_iso(),
                    endpoint=book_path,
                    category="pm_orderbook",
                    key=entry.market_slug,
                    http_status=result.status_code,
                    latency_ms=result.latency_ms,
                    payload=payload,
                )

    def probe(self) -> int:
        series_slug = self._series_slug()
        event_prefix = self._event_prefix()
        today = datetime.now(timezone.utc).date()

        print("=== PROBE Gamma series discovery ===")
        print(
            "query="
            + json.dumps(
                {
                    "series_slug": series_slug,
                    "active": True,
                    "closed": False,
                    "limit": int(self.gamma_cfg.get("discovery_limit") or 10),
                    "order": "endDate",
                    "ascending": True,
                }
            )
        )
        series_result = self.gamma.list_series_events(
            series_slug,
            limit=int(self.gamma_cfg.get("discovery_limit") or 10),
        )
        print(f"status={series_result.status_code}")

        slug, slug_result, event = resolve_event_for_day(
            self.gamma,
            today,
            prefix=event_prefix,
        )
        print(f"=== PROBE Gamma GET /events/slug/{{slug}} (current day {today.isoformat()}) ===")
        print(f"resolved_slug={slug}")
        if slug_result is not None:
            print(f"status={slug_result.status_code}")

        if event is None:
            print("probe: no current-day event resolved")
            return 0

        open_markets = open_bracket_markets(event)
        ladder = bracket_ladder_set(open_markets)

        print("=== PROBE STRUCTURE (logging contract) ===")
        print(
            "market_structure=EXHAUSTIVE_BRACKET_LADDER "
            "(disjoint 2°F bins + tails; negRisk mutually exclusive; Σp≈1 across set)"
        )
        print(f"open_bracket_count={len(open_markets)}")
        print("bracket_set=" + json.dumps(ladder, indent=2))
        neg_risk = any(m.get("negRisk") for m in open_markets)
        print(f"neg_risk_linked={neg_risk}")

        print(f"=== RAW MARKETS current event slug={slug} (open brackets only) ===")
        print(json.dumps(open_markets, indent=2, default=str))

        if open_markets:
            sample_token = None
            for market in open_markets:
                meta = market.get("pm_meta") if isinstance(market.get("pm_meta"), dict) else {}
                token = meta.get("yes_token_id")
                if isinstance(token, str) and token:
                    sample_token = token
                    break
            if sample_token:
                book_result = self.clob.get_book(sample_token)
                print("=== PROBE CLOB GET /book (sample Yes token) ===")
                print("query=" + json.dumps({"token_id": sample_token}))
                print(f"status={book_result.status_code}")
                print(json.dumps(book_result.json_body, indent=2, default=str))

        return 0

    def _restore_ladder_state(self) -> None:
        raw = get_state(self.conn, ACTIVE_LADDER_KEY)
        if not raw:
            return
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(rows, list):
            return
        restored: list[LadderEntry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = row.get("yes_token_id")
            slug = row.get("market_slug")
            event_slug = row.get("event_slug")
            kind = row.get("bracket_kind")
            label = row.get("bracket_label")
            if (
                isinstance(token, str)
                and isinstance(slug, str)
                and isinstance(event_slug, str)
                and isinstance(kind, str)
                and isinstance(label, str)
            ):
                low = row.get("bracket_low_f")
                high = row.get("bracket_high_f")
                restored.append(
                    LadderEntry(
                        event_slug=event_slug,
                        market_slug=slug,
                        yes_token_id=token,
                        bracket_kind=kind,
                        bracket_low_f=low if isinstance(low, int) else None,
                        bracket_high_f=high if isinstance(high, int) else None,
                        bracket_label=label,
                    )
                )
            elif (
                isinstance(token, str)
                and isinstance(slug, str)
                and isinstance(event_slug, str)
                and isinstance(row.get("strike_f"), int)
                and isinstance(row.get("direction"), str)
            ):
                # Legacy state from cumulative-threshold metadata (pre-bracket fix)
                strike = row["strike_f"]
                direction = row["direction"]
                if direction == ">=":
                    is_between = "between" in slug
                    kind = "between" if is_between else "tail_above"
                    high = strike + 1 if is_between else None
                    restored.append(
                        LadderEntry(
                            event_slug=event_slug,
                            market_slug=slug,
                            yes_token_id=token,
                            bracket_kind=kind,
                            bracket_low_f=strike,
                            bracket_high_f=high,
                            bracket_label=str(row.get("bracket_label") or slug),
                        )
                    )
        self._active_ladder = restored

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
    parser = argparse.ArgumentParser(description="Polymarket NYC daily-high temperature logger")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Gamma series discovery + current-day strike ladder + sample CLOB book",
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
