"""Probe taker_outcome_side × taker_book_side on live GET /markets/trades.

Does not assume the book-side frame. Writes nothing to knowledge/; the caller
records NOT_RUN / INCONSISTENT / the observed bijection after a real sample.

This script is a probe, not a backtest path. Network + wall clock are allowed
here only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from wxmm.analysis.trades_ingest import RawTrade, parse_trade
from wxmm.core.errors import InconsistentTakerMapping, PriceComplementError
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping

LIVE_TRADES_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/trades"
MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
HIGHNY_NEEDLES = ("HIGHNY", "KXHIGHNY")


def _looks_like_highny(ticker: str) -> bool:
    upper = ticker.upper()
    return any(needle in upper for needle in HIGHNY_NEEDLES)


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "wxmm-mapping-probe/1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"{url} did not return an object")
    return body


def fetch_live_trades(*, limit: int, ticker: str | None) -> list[dict[str, Any]]:
    params = f"limit={limit}"
    if ticker:
        params += f"&ticker={ticker}"
    body = _get_json(f"{LIVE_TRADES_URL}?{params}")
    rows = body.get("trades") or []
    if not isinstance(rows, list):
        raise ValueError("GET /markets/trades trades field is not a list")
    return [row for row in rows if isinstance(row, dict)]


def fetch_series_tickers(series_ticker: str, *, limit: int = 200) -> list[str]:
    body = _get_json(f"{MARKETS_URL}?series_ticker={series_ticker}&limit={limit}")
    markets = body.get("markets") or []
    if not isinstance(markets, list):
        raise ValueError("GET /markets markets field is not a list")
    tickers: list[str] = []
    for market in markets:
        if isinstance(market, dict) and market.get("ticker"):
            tickers.append(str(market["ticker"]))
    return tickers


def parse_highny_rows(rows: list[dict[str, Any]]) -> tuple[list[RawTrade], list[str]]:
    trades: list[RawTrade] = []
    errors: list[str] = []
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if not _looks_like_highny(ticker):
            continue
        try:
            trades.append(parse_trade(row, source_endpoint="live"))
        except (ValueError, PriceComplementError, KeyError) as exc:
            errors.append(f"{row.get('trade_id')}: {exc}")
    return trades, errors


def run_probe(
    *,
    limit: int,
    ticker: str | None,
    series: str | None = None,
    max_markets: int = 20,
) -> dict[str, Any]:
    try:
        rows: list[dict[str, Any]] = []
        if series:
            tickers = fetch_series_tickers(series)[:max_markets]
            for market_ticker in tickers:
                rows.extend(fetch_live_trades(limit=limit, ticker=market_ticker))
        else:
            rows = fetch_live_trades(limit=limit, ticker=ticker)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "NOT_RUN", "reason": f"live GET /markets/trades failed: {exc}"}
    trades, parse_errors = parse_highny_rows(rows)
    payload: dict[str, Any] = {
        "n_raw_rows": len(rows),
        "n_highny_parsed": len(trades),
        "parse_errors": parse_errors[:20],
    }
    if not trades:
        payload["status"] = "NOT_RUN"
        payload["reason"] = "no parseable HIGHNY/KXHIGHNY trades in sample"
        return payload
    try:
        observed = assert_outcome_bookside_mapping(trades)
    except InconsistentTakerMapping as exc:
        payload["status"] = "INCONSISTENT"
        payload["reason"] = str(exc)
        return payload
    payload["status"] = "clean"
    payload["outcome_to_book"] = observed.outcome_to_book
    payload["counts"] = {f"{a}x{b}": n for (a, b), n in observed.counts.items()}
    payload["n_non_block"] = observed.n_non_block
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe taker_outcome_side × taker_book_side on live trades"
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--ticker", default=None, help="exact ticker; omit to scan the page")
    parser.add_argument(
        "--series",
        default=None,
        help="walk GET /markets?series_ticker=… then per-ticker trades (e.g. KXHIGHNY)",
    )
    parser.add_argument("--max-markets", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_probe(
        limit=args.limit,
        ticker=args.ticker,
        series=args.series,
        max_markets=args.max_markets,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result.get("status") in {"clean", "NOT_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
