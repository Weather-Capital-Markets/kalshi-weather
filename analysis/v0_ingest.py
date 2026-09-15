"""Cutoff-first KXHIGHNY/HIGHNY trade ingest for C1-M1 v0.

Not blocked on X1 FILL_IN. Decision for v0 is a sign test.

python -m analysis.v0_ingest --out-dir analysis/out/v0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    HistoricalCutoff,
    RawTrade,
    fetch_cutoff,
    merge_trades,
    paginate_trades,
    parse_climate_day,
    trades_to_parquet,
)
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping

LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "wxmm-v0-ingest/1"


class KalshiPublicTransport:
    def __init__(self, *, min_interval_s: float = 0.12) -> None:
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = LIVE_BASE + path + (("?" + query) if query else "")
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        finally:
            self._last = time.monotonic()
        if not isinstance(body, dict):
            raise ValueError(f"{path} did not return an object")
        return body


def _iso_ts(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def paginate_markets(
    transport: KalshiPublicTransport, path: str, series: str
) -> list[dict[str, Any]]:
    cursor: str | None = None
    out: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"series_ticker": series, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = transport.get_json(path, params)
        rows = body.get("markets") or []
        if not isinstance(rows, list):
            raise ValueError(f"{path} markets is not a list")
        out.extend(row for row in rows if isinstance(row, dict))
        nxt = body.get("cursor")
        if not nxt or not rows:
            break
        cursor = str(nxt)
    return out


def enumerate_universe(transport: KalshiPublicTransport) -> list[dict[str, Any]]:
    historical = paginate_markets(transport, "/historical/markets", "KXHIGHNY")
    live = paginate_markets(transport, "/markets", "KXHIGHNY")
    by_ticker: dict[str, dict[str, Any]] = {}
    for row in historical + live:
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        try:
            climate = parse_climate_day(ticker)
        except ValueError:
            continue
        if climate < SIX_BRACKET_ERA_START:
            continue
        by_ticker[ticker] = row
    return [by_ticker[key] for key in sorted(by_ticker)]


def pull_one(
    transport: KalshiPublicTransport,
    market: dict[str, Any],
    cutoff: HistoricalCutoff,
) -> tuple[list[RawTrade], list[RawTrade]]:
    ticker = str(market["ticker"])
    close_ts = _iso_ts(market.get("close_time") or market.get("settlement_ts"))
    open_ts = _iso_ts(market.get("open_time"))
    cutoff_ts = float(cutoff.market_settled_ts)
    want_hist = open_ts is None or open_ts < cutoff_ts
    want_live = close_ts is None or close_ts >= cutoff_ts
    historical: list[RawTrade] = []
    live: list[RawTrade] = []
    if want_hist:
        historical = paginate_trades(
            transport,
            path="/historical/trades",
            source_endpoint="historical",
            ticker=ticker,
            max_ts=int(cutoff_ts),
        )
    if want_live:
        live = paginate_trades(
            transport,
            path="/markets/trades",
            source_endpoint="live",
            ticker=ticker,
            min_ts=int(cutoff_ts),
        )
    return historical, live


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C1-M1 v0 cutoff-first trade ingest")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out/v0"))
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--min-volume", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "ingest_progress.jsonl"
    trades_jsonl = out_dir / "trades.jsonl"
    done: set[str] = set()
    if progress_path.is_file():
        for line in progress_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("status") == "ok":
                    done.add(row["ticker"])

    transport = KalshiPublicTransport()
    cutoff = fetch_cutoff(transport)
    (out_dir / "cutoff.json").write_text(json.dumps(cutoff.raw, indent=2) + "\n")
    markets = enumerate_universe(transport)
    (out_dir / "markets.json").write_text(
        json.dumps(
            [
                {
                    "ticker": m.get("ticker"),
                    "strike_type": m.get("strike_type"),
                    "floor_strike": m.get("floor_strike"),
                    "cap_strike": m.get("cap_strike"),
                    "result": m.get("result"),
                    "volume_fp": m.get("volume_fp"),
                    "close_time": m.get("close_time"),
                    "status": m.get("status"),
                }
                for m in markets
            ]
        )
        + "\n"
    )
    eligible = []
    for market in markets:
        try:
            vol = float(market.get("volume_fp") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        ticker = str(market["ticker"])
        if vol <= 0 or vol < args.min_volume:
            if ticker not in done:
                with progress_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "ticker": ticker,
                                "status": "ok",
                                "n_merged": 0,
                                "skipped": "zero_volume",
                            }
                        )
                        + "\n"
                    )
            continue
        eligible.append(market)
    if args.max_tickers is not None:
        eligible = eligible[: args.max_tickers]

    all_trades: list[RawTrade] = []
    n_err = 0
    for i, market in enumerate(eligible, start=1):
        ticker = str(market["ticker"])
        if ticker in done:
            continue
        try:
            historical, live = pull_one(transport, market, cutoff)
            merged, report = merge_trades(historical, live, cutoff)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            n_err += 1
            with progress_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"ticker": ticker, "status": "error", "error": str(exc)}) + "\n"
                )
            print(f"ERR {i}/{len(eligible)} {ticker} {exc}", file=sys.stderr)
            continue
        all_trades.extend(merged)
        with trades_jsonl.open("a", encoding="utf-8") as handle:
            for trade in merged:
                handle.write(
                    json.dumps(
                        {
                            "trade_id": trade.trade_id,
                            "ticker": trade.ticker,
                            "count": str(trade.count),
                            "yes_price": str(trade.yes_price),
                            "no_price": str(trade.no_price),
                            "taker_outcome_side": trade.taker_outcome_side,
                            "taker_book_side": trade.taker_book_side,
                            "created_time": trade.created_time.isoformat(),
                            "is_block_trade": trade.is_block_trade,
                            "source_endpoint": trade.source_endpoint,
                            "climate_day": trade.climate_day.isoformat(),
                            "ladder_regime": trade.ladder_regime,
                            "settlement_rule_id": trade.settlement_rule_id,
                            "close_time_convention": trade.close_time_convention,
                        }
                    )
                    + "\n"
                )
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ticker": ticker,
                        "status": "ok",
                        "n_historical": report.n_historical,
                        "n_live": report.n_live,
                        "n_merged": report.n_merged,
                        "n_duplicate_ids": report.n_duplicate_ids,
                        "gap_note": report.gap_note,
                    }
                )
                + "\n"
            )
        if i % 25 == 0 or i == len(eligible):
            print(
                f"{i}/{len(eligible)} {ticker} merged={report.n_merged} running={len(all_trades)}",
                file=sys.stderr,
            )

    # Reload from progress is incomplete if we skipped done tickers without storing trades.
    # Full parquet is written only for this process's newly pulled trades plus we re-pull
    # skipped ones only when progress is empty. For resume, caller should not expect
    # all_trades to include prior tickers; write a shard.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shard = out_dir / f"trades_{stamp}.parquet"
    if all_trades:
        trades_to_parquet(all_trades, shard)
    mapping_status: dict[str, Any]
    try:
        observed = assert_outcome_bookside_mapping(
            [t for t in all_trades if not t.is_block_trade]
        )
        mapping_status = {
            "status": "clean",
            "outcome_to_book": observed.outcome_to_book,
            "counts": {f"{a}x{b}": n for (a, b), n in observed.counts.items()},
            "n_non_block": observed.n_non_block,
        }
    except Exception as exc:  # noqa: BLE001 — probe must record INCONSISTENT
        mapping_status = {"status": "INCONSISTENT", "reason": str(exc)}
    summary = {
        "cutoff": cutoff.raw,
        "n_markets": len(markets),
        "n_eligible": len(eligible),
        "n_pulled_this_run": len({t.ticker for t in all_trades}),
        "n_trades_this_run": len(all_trades),
        "n_errors": n_err,
        "mapping": mapping_status,
        "shard": str(shard) if all_trades else None,
        "progress_path": str(progress_path),
        "note": "v0 ingest is not gated on X1 FILL_IN; decision is a sign test",
    }
    (out_dir / "ingest_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if mapping_status.get("status") in {"clean", "INCONSISTENT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
