"""S2 relative-value census: executable book-coherence on frozen candles.

Allowed DB: none. Frozen markets_history + candlesticks only. No network,
no NBM, no models, no trading logic.

Two phases so coverage can be pre-registered before violation magnitudes:

  --phase coverage     complete-book rates, band mix, size-field coverage
  --phase violations   executable sums, persistence, capital, figures
  --phase all          both (after the threshold is written)

Prints distributions only — no verdict language.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import (
    _float,
    candle_fields,
    is_stale,
    is_two_sided,
    last_candle_at_or_before,
    load_markets,
    parse_iso_utc,
    season_of,
    ticker_climate_date,
)
from ingestion.climate_day import NY_CIVIL, climate_day_end
from ingestion.config_loader import load_config
from ingestion.writer import read_jsonl_gz

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

START_DATE = date(2022, 12, 11)
HORIZONS_H = (36, 24, 12, 6)
N_BRACKETS = 6
BAND_LO = 0.10
BAND_HI = 0.90
SIZE_NS = (1, 5, 10, 25)
NO_PAYOUT = float(N_BRACKETS - 1)
ERA_10AM_LAST = date(2024, 9, 3)
Phase = Literal["coverage", "violations", "all"]

NOTE_UNIVERSE = (
    "NOTE: universe is climate dates >= 2022-12-11 with exactly 6 KXHIGHNY/HIGHNY "
    "markets (stable 6-bracket regime). This is the full candle history, not the "
    "300-day NBM sample. The K2 71/300 figure was two-sided mid-sums at T-24h on "
    "that sample and is not a denominator here."
)
NOTE_COMPLETE = (
    "NOTE: a complete book requires every bracket two-sided (bid>=$0.01, ask<=$0.99) "
    "and the snapshot inside every leg's trading window. A partial set is not a "
    "coherent basket. Primary quotes are carry-forward; `_strict15` is robustness."
)
NOTE_NO_BASKET = (
    "NOTE: buying all six NO pays $5 when five of six resolve Yes. Cost is "
    "sum(NO ask). Explicit candle no_ask is used when present; otherwise "
    "NO ask = 1 - YES bid (the implied short). Implied NO is mechanically the "
    "gross (no-netting) YES-sell; explicit NO may differ."
)
NOTE_DEPTH = (
    "NOTE: historical candles carry bid/ask close, volume, and open interest — not "
    "resting depth. 1-lot is assumed at the displayed quote (unconfirmed). "
    "5/10/25-lot columns are NA unless every relevant size field is present and "
    ">= n. Candle volume is period volume, not depth. Logger books are required "
    "for a real size answer."
)
NOTE_NETTING = (
    "NOTE: whether Kalshi nets offsetting positions across brackets in one event "
    "is UNVERIFIED (venue-facts §2.2 placeholder; data-sources collateral-netting "
    "open item). Sell-side capital is printed three ways and none is chosen: "
    "gross 6-sum_bid; netted $1; netted max(1-sum_bid, 0)."
)
NOTE_FEE = (
    "NOTE: taker-fee robustness uses ceil_6dp(0.07*P*(1-P)) per leg (published "
    "quadratic schedule, not live-page verified). Maker fee on KXHIGHNY was $0 "
    "as of 2026-08-15."
)
TAKER_COEFF = 0.07


def ceil_6dp(value: float) -> float:
    return math.ceil(value * 1_000_000.0 - 1e-12) / 1_000_000.0


def quadratic_taker_fee(price: float, *, coeff: float = TAKER_COEFF) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return ceil_6dp(coeff * price * (1.0 - price))


NOTE_PHASE = (
    "=== STOP: coverage only. Re-run with --phase violations after the "
    "pre-registration threshold is written. This file and printout contain no "
    "violation magnitudes. ==="
)

COVERAGE_COLUMNS = [
    "climate_date",
    "horizon_h",
    "season",
    "n_brackets",
    "n_two_sided",
    "n_two_sided_strict15",
    "in_window",
    "complete_book",
    "complete_book_strict15",
    "complete_book_unconditional",
    "n_in_band_10_90",
    "all_in_band",
    "n_outside_band",
    "n_one_cent_quote",
    "n_explicit_no_ask",
    "n_legs_with_ask_size",
    "n_legs_with_bid_size",
    "max_quote_age_sec",
]


@dataclass(frozen=True)
class LegQuote:
    ticker: str
    bid: float | None
    ask: float | None
    no_bid: float | None
    no_ask: float | None
    no_ask_source: str
    bid_size: float | None
    ask_size: float | None
    no_ask_size: float | None
    end_period_ts: int | None
    two_sided: bool
    two_sided_strict15: bool
    mid: float | None
    in_band: bool
    one_cent: bool


@dataclass
class BasketSnapshot:
    climate_date: str
    horizon_h: int
    season: str
    snapshot_ts: int
    in_window: bool
    legs: tuple[LegQuote, ...]
    n_two_sided: int
    n_two_sided_strict15: int
    n_in_band: int
    n_outside_band: int
    n_one_cent: int
    n_explicit_no_ask: int
    n_legs_with_ask_size: int
    n_legs_with_bid_size: int
    max_quote_age_sec: float | None
    complete_unconditional: bool
    complete_book: bool
    complete_strict15: bool
    all_in_band: bool


def _size_from(obj: Any, candle: dict[str, Any], prefix: str) -> float | None:
    if isinstance(obj, dict):
        for key in ("close_size_fp", "close_size", "size_fp", "size"):
            value = _float(obj.get(key))
            if value is not None:
                return value
    for key in (f"{prefix}_size_fp", f"{prefix}_size"):
        value = _float(candle.get(key))
        if value is not None:
            return value
    return None


def extended_candle_fields(candle: dict[str, Any]) -> dict[str, Any]:
    fields = candle_fields(candle)
    yes_bid = candle.get("yes_bid") if isinstance(candle.get("yes_bid"), dict) else {}
    yes_ask = candle.get("yes_ask") if isinstance(candle.get("yes_ask"), dict) else {}
    no_bid = candle.get("no_bid") if isinstance(candle.get("no_bid"), dict) else {}
    no_ask = candle.get("no_ask") if isinstance(candle.get("no_ask"), dict) else {}
    fields["no_bid_close"] = _float(no_bid.get("close_dollars", no_bid.get("close")))
    fields["no_ask_close"] = _float(no_ask.get("close_dollars", no_ask.get("close")))
    fields["bid_size"] = _size_from(yes_bid, candle, "yes_bid")
    fields["ask_size"] = _size_from(yes_ask, candle, "yes_ask")
    fields["no_ask_size"] = _size_from(no_ask, candle, "no_ask")
    return fields


def implied_no_ask(yes_bid: float | None) -> float | None:
    if yes_bid is None:
        return None
    return 1.0 - yes_bid


def implied_no_bid(yes_ask: float | None) -> float | None:
    if yes_ask is None:
        return None
    return 1.0 - yes_ask


def resolve_no_ask(explicit: float | None, yes_bid: float | None) -> tuple[float | None, str]:
    if explicit is not None:
        return explicit, "explicit"
    implied = implied_no_ask(yes_bid)
    if implied is None:
        return None, "missing"
    return implied, "implied"


def basket_sums(asks: list[float], bids: list[float], no_asks: list[float]) -> dict[str, float]:
    """Executable basket arithmetic. Length must be N_BRACKETS."""
    if len(asks) != N_BRACKETS or len(bids) != N_BRACKETS or len(no_asks) != N_BRACKETS:
        raise ValueError("basket_sums requires six legs")
    sum_ask = float(sum(asks))
    sum_bid = float(sum(bids))
    sum_no_ask = float(sum(no_asks))
    return {
        "sum_ask": sum_ask,
        "sum_bid": sum_bid,
        "sum_no_ask": sum_no_ask,
        "slack_ask": 1.0 - sum_ask,
        "slack_bid": sum_bid - 1.0,
        "slack_no": NO_PAYOUT - sum_no_ask,
    }


def penny_excluded_arithmetic(
    asks: list[float],
    bids: list[float],
    *,
    orig_buy: bool,
    orig_sell: bool,
) -> dict[str, float | bool | int | None]:
    """Recompute executable sums on legs that are not bid<=1c or ask>=99c.

    Slack is still versus 1 (MECE book). A subset sell (sum remaining bids > 1)
    or subset buy (sum remaining asks < 1) is a surviving violation. If the
    original violation is gone after the drop, slack_from_penny_leg is True.
    Empty remainder (every leg a penny quote) cannot violate; the original
    slack is then from penny legs.
    """
    if len(asks) != N_BRACKETS or len(bids) != N_BRACKETS:
        raise ValueError("penny_excluded_arithmetic requires six legs")
    keep_asks: list[float] = []
    keep_bids: list[float] = []
    for ask, bid in zip(asks, bids, strict=True):
        if _one_cent(bid, ask):
            continue
        keep_asks.append(ask)
        keep_bids.append(bid)
    n_keep = len(keep_asks)
    n_penny = len(asks) - n_keep
    if n_keep == 0:
        return {
            "n_non_penny_legs": 0,
            "n_penny_legs": n_penny,
            "sum_ask_excl_penny": None,
            "sum_bid_excl_penny": None,
            "slack_ask_excl_penny": None,
            "slack_bid_excl_penny": None,
            "penny_excluded_buy_violation": False,
            "penny_excluded_sell_violation": False,
            "slack_from_penny_leg": bool(orig_buy or orig_sell),
        }
    sum_ask = float(sum(keep_asks))
    sum_bid = float(sum(keep_bids))
    slack_ask = 1.0 - sum_ask
    slack_bid = sum_bid - 1.0
    pe_buy = slack_ask > 0.0
    pe_sell = slack_bid > 0.0
    return {
        "n_non_penny_legs": n_keep,
        "n_penny_legs": n_penny,
        "sum_ask_excl_penny": sum_ask,
        "sum_bid_excl_penny": sum_bid,
        "slack_ask_excl_penny": slack_ask,
        "slack_bid_excl_penny": slack_bid,
        "penny_excluded_buy_violation": pe_buy,
        "penny_excluded_sell_violation": pe_sell,
        "slack_from_penny_leg": bool((orig_buy and not pe_buy) or (orig_sell and not pe_sell)),
    }


def taker_adjusted_sums(
    asks: list[float], bids: list[float], no_asks: list[float]
) -> dict[str, float]:
    raw = basket_sums(asks, bids, no_asks)
    sum_ask_fee = float(sum(a + quadratic_taker_fee(a) for a in asks))
    sum_bid_fee = float(sum(b - quadratic_taker_fee(b) for b in bids))
    sum_no_fee = float(sum(n + quadratic_taker_fee(n) for n in no_asks))
    raw["sum_ask_after_fee"] = sum_ask_fee
    raw["sum_bid_after_fee"] = sum_bid_fee
    raw["sum_no_ask_after_fee"] = sum_no_fee
    raw["slack_ask_after_fee"] = 1.0 - sum_ask_fee
    raw["slack_bid_after_fee"] = sum_bid_fee - 1.0
    raw["slack_no_after_fee"] = NO_PAYOUT - sum_no_fee
    return raw


def collateral_arithmetic(
    sum_ask: float, sum_bid: float, sum_no_ask: float
) -> dict[str, float | None]:
    """Capital and per-trade ROC. Netted sell formulas are both returned, neither chosen."""
    buy_capital = sum_ask
    buy_roc = (1.0 - sum_ask) / buy_capital if buy_capital > 0 else None
    sell_gross_capital = float(N_BRACKETS) - sum_bid
    sell_gross_roc = (sum_bid - 1.0) / sell_gross_capital if sell_gross_capital > 0 else None
    sell_netted_1 = 1.0
    sell_netted_1_roc = (sum_bid - 1.0) / sell_netted_1
    sell_netted_offset = max(1.0 - sum_bid, 0.0)
    sell_netted_offset_roc = (
        (sum_bid - 1.0) / sell_netted_offset if sell_netted_offset > 0 else None
    )
    no_capital = sum_no_ask
    no_roc = (NO_PAYOUT - sum_no_ask) / no_capital if no_capital > 0 else None
    return {
        "capital_buy": buy_capital,
        "roc_buy": buy_roc,
        "capital_sell_gross": sell_gross_capital,
        "roc_sell_gross": sell_gross_roc,
        "capital_sell_netted_1": sell_netted_1,
        "roc_sell_netted_1": sell_netted_1_roc,
        "capital_sell_netted_offset": sell_netted_offset,
        "roc_sell_netted_offset": sell_netted_offset_roc,
        "capital_buy_no": no_capital,
        "roc_buy_no": no_roc,
    }


def annualize_simple(roc: float | None, holding_hours: float | None) -> float | None:
    if roc is None or holding_hours is None or holding_hours <= 0:
        return None
    return roc * (8760.0 / holding_hours)


def complete_book_filter(two_sided: list[bool]) -> bool:
    return len(two_sided) == N_BRACKETS and all(two_sided)


def fallback_settlement_utc(climate_date: str) -> tuple[datetime, str]:
    day = date.fromisoformat(climate_date)
    next_day = day + timedelta(days=1)
    if day <= ERA_10AM_LAST:
        hour, source = 10, "era_10am_et"
    else:
        hour, source = 8, "era_8am_et"
    naive = datetime(next_day.year, next_day.month, next_day.day, hour, 0)
    return naive.replace(tzinfo=NY_CIVIL).astimezone(timezone.utc), source


def settlement_of_event(markets: list[dict[str, Any]], climate_date: str) -> tuple[datetime, str]:
    stamps: list[datetime] = []
    for market in markets:
        parsed = parse_iso_utc(
            str(market.get("settlement_ts") or "") if market.get("settlement_ts") else None
        )
        if parsed is not None:
            stamps.append(parsed)
    if stamps:
        return max(stamps), "market_settlement_ts"
    return fallback_settlement_utc(climate_date)


def _one_cent(bid: float | None, ask: float | None) -> bool:
    if bid is not None and bid <= 0.01 + 1e-12:
        return True
    if ask is not None and ask >= 0.99 - 1e-12:
        return True
    return False


def _leg_in_window(market: dict[str, Any], snapshot: datetime) -> bool:
    open_raw = market.get("open_time")
    close_raw = market.get("close_time")
    open_dt = parse_iso_utc(open_raw if isinstance(open_raw, str) else None)
    close_dt = parse_iso_utc(close_raw if isinstance(close_raw, str) else None)
    if open_dt is None and close_dt is None:
        return True
    return (open_dt is None or snapshot >= open_dt) and (close_dt is None or snapshot <= close_dt)


def quote_leg(
    ticker: str,
    candle: dict[str, Any] | None,
    snapshot_ts: int,
) -> LegQuote:
    bid = candle["bid_close"] if candle else None
    ask = candle["ask_close"] if candle else None
    explicit_no_ask = candle["no_ask_close"] if candle else None
    explicit_no_bid = candle["no_bid_close"] if candle else None
    no_ask, source = resolve_no_ask(explicit_no_ask, bid)
    no_bid = explicit_no_bid if explicit_no_bid is not None else implied_no_bid(ask)
    two = bool(candle) and is_two_sided(bid, ask)
    stale = True if candle is None else is_stale(candle, snapshot_ts)
    two_strict = two and not stale
    mid = (bid + ask) / 2.0 if two and bid is not None and ask is not None else None
    in_band = mid is not None and BAND_LO <= mid <= BAND_HI
    end_ts = (
        int(candle["end_period_ts"]) if candle and candle.get("end_period_ts") is not None else None
    )
    return LegQuote(
        ticker=ticker,
        bid=bid,
        ask=ask,
        no_bid=no_bid,
        no_ask=no_ask,
        no_ask_source=source,
        bid_size=candle.get("bid_size") if candle else None,
        ask_size=candle.get("ask_size") if candle else None,
        no_ask_size=candle.get("no_ask_size") if candle else None,
        end_period_ts=end_ts,
        two_sided=two,
        two_sided_strict15=two_strict,
        mid=mid,
        in_band=in_band,
        one_cent=_one_cent(bid, ask),
    )


def six_bracket_universe(
    markets: list[dict[str, Any]],
    *,
    start: date = START_DATE,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate_date = ticker_climate_date(ticker)
        if climate_date is None:
            continue
        if date.fromisoformat(climate_date) < start:
            continue
        by_day.setdefault(climate_date, []).append(market)
    six: dict[str, list[dict[str, Any]]] = {}
    other: dict[str, int] = {}
    for climate_date, group in by_day.items():
        n_unique = len({str(m.get("ticker") or "") for m in group})
        if n_unique == N_BRACKETS:
            by_ticker = {str(m.get("ticker") or ""): m for m in group}
            six[climate_date] = list(by_ticker.values())
        else:
            other[climate_date] = n_unique
    return six, other


def load_extended_candles(raw_dir: Path, tickers: set[str]) -> dict[str, list[dict[str, Any]]]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    paths = [
        path
        for path in raw_dir.glob("*/candlesticks/*.jsonl.gz")
        if path.name.removesuffix(".jsonl.gz") in tickers
    ]
    logger.info("candle files matching universe=%s", len(paths))
    for i, path in enumerate(paths, start=1):
        if i % 500 == 0:
            logger.info("candle files %s/%s", i, len(paths))
        file_ticker = path.name.removesuffix(".jsonl.gz")
        for record in read_jsonl_gz(path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            ticker = str(payload.get("ticker") or file_ticker)
            if ticker not in tickers:
                continue
            candles = payload.get("candlesticks")
            if not isinstance(candles, list):
                continue
            bucket = by_ticker.setdefault(ticker, [])
            for candle in candles:
                if isinstance(candle, dict):
                    bucket.append(extended_candle_fields(candle))
    return by_ticker


CandleIndex = dict[str, tuple[list[int], list[dict[str, Any]]]]


def build_candle_index(
    candles_by_ticker: dict[str, list[dict[str, Any]]],
) -> CandleIndex:
    index: CandleIndex = {}
    for ticker, candles in candles_by_ticker.items():
        valid = [c for c in candles if isinstance(c.get("end_period_ts"), (int, float))]
        valid.sort(key=lambda c: int(c["end_period_ts"]))
        index[ticker] = ([int(c["end_period_ts"]) for c in valid], valid)
    return index


def last_indexed(index: CandleIndex, ticker: str, snapshot_ts: int) -> dict[str, Any] | None:
    times, ordered = index.get(ticker, ([], []))
    idx = bisect.bisect_right(times, snapshot_ts) - 1
    if idx < 0:
        return None
    return ordered[idx]


def _build_legs(
    markets: list[dict[str, Any]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
    snapshot_ts: int,
    candle_index: CandleIndex | None = None,
) -> tuple[LegQuote, ...]:
    legs: list[LegQuote] = []
    for market in sorted(markets, key=lambda m: str(m.get("ticker") or "")):
        ticker = str(market.get("ticker") or "")
        if candle_index is not None:
            candle = last_indexed(candle_index, ticker, snapshot_ts)
        else:
            candle = last_candle_at_or_before(candles_by_ticker.get(ticker, []), snapshot_ts)
        legs.append(quote_leg(ticker, candle, snapshot_ts))
    return tuple(legs)


def assemble_basket(
    *,
    climate_date: str,
    horizon_h: int,
    markets: list[dict[str, Any]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
) -> BasketSnapshot:
    t_end = climate_day_end(date.fromisoformat(climate_date))
    snapshot = t_end - timedelta(hours=horizon_h)
    snapshot_ts = int(snapshot.timestamp())
    in_window = all(_leg_in_window(market, snapshot) for market in markets)
    legs = _build_legs(markets, candles_by_ticker, snapshot_ts)
    n_two = sum(1 for leg in legs if leg.two_sided)
    n_strict = sum(1 for leg in legs if leg.two_sided_strict15)
    n_band = sum(1 for leg in legs if leg.in_band)
    n_out = sum(1 for leg in legs if leg.two_sided and not leg.in_band)
    n_cent = sum(1 for leg in legs if leg.one_cent)
    n_explicit = sum(1 for leg in legs if leg.no_ask_source == "explicit")
    n_ask_sz = sum(1 for leg in legs if leg.ask_size is not None)
    n_bid_sz = sum(1 for leg in legs if leg.bid_size is not None)
    ages = [float(snapshot_ts - leg.end_period_ts) for leg in legs if leg.end_period_ts is not None]
    complete_uncond = complete_book_filter([leg.two_sided for leg in legs])
    complete = bool(in_window and complete_uncond)
    complete_s15 = bool(
        in_window and complete_book_filter([leg.two_sided_strict15 for leg in legs])
    )
    all_in_band = bool(complete and n_band == N_BRACKETS)
    return BasketSnapshot(
        climate_date=climate_date,
        horizon_h=horizon_h,
        season=season_of(climate_date),
        snapshot_ts=snapshot_ts,
        in_window=in_window,
        legs=legs,
        n_two_sided=n_two,
        n_two_sided_strict15=n_strict,
        n_in_band=n_band,
        n_outside_band=n_out,
        n_one_cent=n_cent,
        n_explicit_no_ask=n_explicit,
        n_legs_with_ask_size=n_ask_sz,
        n_legs_with_bid_size=n_bid_sz,
        max_quote_age_sec=max(ages) if ages else None,
        complete_unconditional=complete_uncond,
        complete_book=complete,
        complete_strict15=complete_s15,
        all_in_band=all_in_band,
    )


def coverage_row(basket: BasketSnapshot) -> dict[str, Any]:
    return {
        "climate_date": basket.climate_date,
        "horizon_h": basket.horizon_h,
        "season": basket.season,
        "n_brackets": N_BRACKETS,
        "n_two_sided": basket.n_two_sided,
        "n_two_sided_strict15": basket.n_two_sided_strict15,
        "in_window": basket.in_window,
        "complete_book": basket.complete_book,
        "complete_book_strict15": basket.complete_strict15,
        "complete_book_unconditional": basket.complete_unconditional,
        "n_in_band_10_90": basket.n_in_band,
        "all_in_band": basket.all_in_band,
        "n_outside_band": basket.n_outside_band,
        "n_one_cent_quote": basket.n_one_cent,
        "n_explicit_no_ask": basket.n_explicit_no_ask,
        "n_legs_with_ask_size": basket.n_legs_with_ask_size,
        "n_legs_with_bid_size": basket.n_legs_with_bid_size,
        "max_quote_age_sec": basket.max_quote_age_sec,
    }


def complete_quotes(basket: BasketSnapshot) -> tuple[list[float], list[float], list[float]] | None:
    if not basket.complete_book:
        return None
    asks: list[float] = []
    bids: list[float] = []
    no_asks: list[float] = []
    for leg in basket.legs:
        if leg.ask is None or leg.bid is None or leg.no_ask is None:
            return None
        asks.append(leg.ask)
        bids.append(leg.bid)
        no_asks.append(leg.no_ask)
    return asks, bids, no_asks


def size_constrained_slack(
    basket: BasketSnapshot,
    slack_ask: float,
    slack_bid: float,
    slack_no: float,
) -> dict[str, float | None]:
    """1-lot assumed; 5/10/25 only when every relevant size exists and is >= n."""
    out: dict[str, float | None] = {}
    ask_sizes = [leg.ask_size for leg in basket.legs]
    bid_sizes = [leg.bid_size for leg in basket.legs]
    no_sizes = [leg.no_ask_size for leg in basket.legs]
    for n in SIZE_NS:
        if n == 1:
            out[f"slack_ask_{n}lot"] = slack_ask
            out[f"slack_bid_{n}lot"] = slack_bid
            out[f"slack_no_{n}lot"] = slack_no
            continue
        out[f"slack_ask_{n}lot"] = (
            n * slack_ask if all(s is not None and s >= n for s in ask_sizes) else None
        )
        out[f"slack_bid_{n}lot"] = (
            n * slack_bid if all(s is not None and s >= n for s in bid_sizes) else None
        )
        out[f"slack_no_{n}lot"] = (
            n * slack_no if all(s is not None and s >= n for s in no_sizes) else None
        )
    out["ask_size_min"] = min((s for s in ask_sizes if s is not None), default=None)
    out["bid_size_min"] = min((s for s in bid_sizes if s is not None), default=None)
    return out


def _state_at_or_before(
    events: list[tuple[int, bool]], snapshot_ts: int
) -> tuple[int, bool] | None:
    eligible = [row for row in events if row[0] <= snapshot_ts]
    if not eligible:
        return None
    return eligible[-1]


def violation_run(
    events: list[tuple[int, bool]],
    snapshot_ts: int,
    close_ts: int,
) -> tuple[int | None, float | None]:
    """Return (n_consecutive_true_events, standing_duration_sec) or (None, None).

    Duration runs from the first true event in the contiguous block to the next
    false event (or close if the block is still true). That is wall-clock time
    the carry-forward book stayed in violation, not just the count of prints.
    """
    if not events:
        return None, None
    current = _state_at_or_before(events, snapshot_ts)
    if current is None or not current[1]:
        return None, None
    idx = max(i for i, row in enumerate(events) if row[0] <= snapshot_ts)
    start = idx
    while start > 0 and events[start - 1][1]:
        start -= 1
    end = idx
    while end + 1 < len(events) and events[end + 1][1]:
        end += 1
    n_events = end - start + 1
    next_false_ts = close_ts
    for later in events[end + 1 :]:
        if not later[1]:
            next_false_ts = later[0]
            break
    duration = float(next_false_ts - events[start][0])
    return n_events, duration


def _buy_violation(basket: BasketSnapshot) -> bool:
    quotes = complete_quotes(basket)
    if quotes is None:
        return False
    return basket_sums(*quotes)["slack_ask"] > 0.0


def _sell_violation(basket: BasketSnapshot) -> bool:
    quotes = complete_quotes(basket)
    if quotes is None:
        return False
    return basket_sums(*quotes)["slack_bid"] > 0.0


def _no_violation(basket: BasketSnapshot) -> bool:
    quotes = complete_quotes(basket)
    if quotes is None:
        return False
    return basket_sums(*quotes)["slack_no"] > 0.0


def union_event_flags_all(
    markets: list[dict[str, Any]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
    climate_date: str,
    candle_index: CandleIndex | None = None,
) -> tuple[list[tuple[int, bool]], list[tuple[int, bool]], list[tuple[int, bool]]]:
    """One union-tape walk; buy / sell / NO flags together."""
    tickers = [str(m.get("ticker") or "") for m in markets]
    times: set[int] = set()
    t_end = climate_day_end(date.fromisoformat(climate_date))
    if candle_index is not None:
        for ticker in tickers:
            times.update(candle_index.get(ticker, ([], []))[0])
    else:
        for ticker in tickers:
            for candle in candles_by_ticker.get(ticker, []):
                ts = candle.get("end_period_ts")
                if isinstance(ts, (int, float)):
                    times.add(int(ts))
    buy_flags: list[tuple[int, bool]] = []
    sell_flags: list[tuple[int, bool]] = []
    no_flags: list[tuple[int, bool]] = []
    for ts in sorted(times):
        snapshot = datetime.fromtimestamp(ts, tz=timezone.utc)
        in_window = all(_leg_in_window(market, snapshot) for market in markets)
        if not in_window or snapshot > t_end:
            continue
        legs = _build_legs(markets, candles_by_ticker, ts, candle_index=candle_index)
        n_two = sum(1 for leg in legs if leg.two_sided)
        complete = in_window and complete_book_filter([leg.two_sided for leg in legs])
        basket = BasketSnapshot(
            climate_date=climate_date,
            horizon_h=0,
            season=season_of(climate_date),
            snapshot_ts=ts,
            in_window=in_window,
            legs=legs,
            n_two_sided=n_two,
            n_two_sided_strict15=sum(1 for leg in legs if leg.two_sided_strict15),
            n_in_band=sum(1 for leg in legs if leg.in_band),
            n_outside_band=sum(1 for leg in legs if leg.two_sided and not leg.in_band),
            n_one_cent=sum(1 for leg in legs if leg.one_cent),
            n_explicit_no_ask=sum(1 for leg in legs if leg.no_ask_source == "explicit"),
            n_legs_with_ask_size=sum(1 for leg in legs if leg.ask_size is not None),
            n_legs_with_bid_size=sum(1 for leg in legs if leg.bid_size is not None),
            max_quote_age_sec=None,
            complete_unconditional=complete,
            complete_book=complete,
            complete_strict15=False,
            all_in_band=complete and sum(1 for leg in legs if leg.in_band) == N_BRACKETS,
        )
        buy_flags.append((ts, bool(complete and _buy_violation(basket))))
        sell_flags.append((ts, bool(complete and _sell_violation(basket))))
        no_flags.append((ts, bool(complete and _no_violation(basket))))
    return buy_flags, sell_flags, no_flags


def violation_row(
    basket: BasketSnapshot,
    markets: list[dict[str, Any]],
    persistence: dict[str, tuple[int | None, float | None]],
) -> dict[str, Any]:
    row = coverage_row(basket)
    quotes = complete_quotes(basket)
    if quotes is None:
        return row
    asks, bids, no_asks = quotes
    sums = taker_adjusted_sums(asks, bids, no_asks)
    capital = collateral_arithmetic(sums["sum_ask"], sums["sum_bid"], sums["sum_no_ask"])
    settle_dt, settle_source = settlement_of_event(markets, basket.climate_date)
    snapshot_dt = datetime.fromtimestamp(basket.snapshot_ts, tz=timezone.utc)
    holding_hours = (settle_dt - snapshot_dt).total_seconds() / 3600.0
    size_cols = size_constrained_slack(
        basket, sums["slack_ask"], sums["slack_bid"], sums["slack_no"]
    )
    n_explicit = basket.n_explicit_no_ask
    implied = [implied_no_ask(b) for b in bids]
    no_disagree = None
    if n_explicit == N_BRACKETS and all(x is not None for x in implied):
        no_disagree = float(sum(no_asks) - sum(implied))  # type: ignore[arg-type]
    buy_n, buy_dur = persistence.get("buy", (None, None))
    sell_n, sell_dur = persistence.get("sell", (None, None))
    no_n, no_dur = persistence.get("no", (None, None))
    row.update(
        {
            **sums,
            **capital,
            **size_cols,
            "buy_violation": sums["slack_ask"] > 0.0,
            "sell_violation": sums["slack_bid"] > 0.0,
            "no_violation": sums["slack_no"] > 0.0,
            "holding_hours": holding_hours,
            "settlement_source": settle_source,
            "roc_buy_ann_simple": annualize_simple(capital["roc_buy"], holding_hours),
            "roc_sell_gross_ann_simple": annualize_simple(capital["roc_sell_gross"], holding_hours),
            "roc_buy_no_ann_simple": annualize_simple(capital["roc_buy_no"], holding_hours),
            "no_ask_minus_implied": no_disagree,
            "n_explicit_no_ask": n_explicit,
            "buy_run_n_candles": buy_n,
            "buy_run_duration_sec": buy_dur,
            "sell_run_n_candles": sell_n,
            "sell_run_duration_sec": sell_dur,
            "no_run_n_candles": no_n,
            "no_run_duration_sec": no_dur,
        }
    )
    orig_buy = bool(sums["slack_ask"] > 0.0)
    orig_sell = bool(sums["slack_bid"] > 0.0)
    row.update(penny_excluded_arithmetic(asks, bids, orig_buy=orig_buy, orig_sell=orig_sell))
    return row


def _rate(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    return float(frame[column].mean())


def _print_coverage_block(coverage: pd.DataFrame, n_other: int, other_counts: Counter[int]) -> None:
    print(NOTE_UNIVERSE)
    print(NOTE_COMPLETE)
    print(
        f"universe_days={coverage['climate_date'].nunique()} "
        f"other_n_days={n_other} other_n_counts={dict(other_counts)}"
    )
    print("\n=== Coverage (complete-book rate; no violation magnitudes) ===")
    seasons = ["DJF", "MAM", "JJA", "SON"]
    print(
        "horizon  n_in_window  n_complete  rate  rate_strict15  rate_uncond  "
        "n_all_in_band  frac_all_in_band  frac_has_1c  frac_size"
    )
    for horizon in HORIZONS_H:
        slice_h = coverage[coverage["horizon_h"] == horizon]
        in_win = slice_h[slice_h["in_window"]]
        complete = in_win[in_win["complete_book"]]
        n_in = len(in_win)
        n_c = int(in_win["complete_book"].sum()) if n_in else 0
        n_band = int(complete["all_in_band"].sum()) if len(complete) else 0
        has_cent = float((complete["n_one_cent_quote"] > 0).mean()) if len(complete) else None
        has_size = (
            float((complete["n_legs_with_ask_size"] == N_BRACKETS).mean())
            if len(complete)
            else None
        )
        print(
            f"T-{horizon:>2}h  {n_in:5d}  {n_c:5d}  "
            f"{(n_c / n_in if n_in else float('nan')):0.3f}  "
            f"{_rate(in_win, 'complete_book_strict15') or float('nan'):0.3f}  "
            f"{_rate(slice_h, 'complete_book_unconditional') or float('nan'):0.3f}  "
            f"{n_band:5d}  "
            f"{(n_band / n_c if n_c else float('nan')):0.3f}  "
            f"{(has_cent if has_cent is not None else float('nan')):0.3f}  "
            f"{(has_size if has_size is not None else float('nan')):0.3f}"
        )
    print("\n=== Coverage by horizon x season (complete rate among in-window) ===")
    print("horizon  season  n_in_window  n_complete  rate  n_all_in_band  frac_all_in_band")
    for horizon in HORIZONS_H:
        for season in seasons:
            slice_hs = coverage[(coverage["horizon_h"] == horizon) & (coverage["season"] == season)]
            in_win = slice_hs[slice_hs["in_window"]]
            n_in = len(in_win)
            n_c = int(in_win["complete_book"].sum()) if n_in else 0
            complete = in_win[in_win["complete_book"]]
            n_band = int(complete["all_in_band"].sum()) if len(complete) else 0
            print(
                f"T-{horizon:>2}h  {season}  {n_in:5d}  {n_c:5d}  "
                f"{(n_c / n_in if n_in else float('nan')):0.3f}  "
                f"{n_band:5d}  "
                f"{(n_band / n_c if n_c else float('nan')):0.3f}"
            )


def _dist_line(label: str, series: pd.Series) -> None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        print(f"{label} n=0")
        return
    print(
        f"{label} n={len(values)} "
        f"p10={values.quantile(0.10):.4f} p50={values.median():.4f} "
        f"p90={values.quantile(0.90):.4f} mean={values.mean():.4f}"
    )


def print_violations(frame: pd.DataFrame) -> None:
    print(NOTE_NO_BASKET)
    print(NOTE_DEPTH)
    print(NOTE_NETTING)
    print(NOTE_FEE)
    complete = frame[frame["complete_book"]].copy()
    print(f"\n=== Complete books (executable sums) n={len(complete)} ===")
    if complete.empty:
        return
    in_band = complete[complete["all_in_band"]]
    print(
        f"complete n={len(complete)} all_in_band n={len(in_band)} "
        f"frac_all_in_band={len(in_band) / len(complete):.3f}"
    )
    for horizon in HORIZONS_H:
        slice_h = complete[complete["horizon_h"] == horizon]
        if slice_h.empty:
            continue
        print(f"\n--- T-{horizon}h complete n={len(slice_h)} ---")
        _dist_line("sum_ask", slice_h["sum_ask"])
        _dist_line("sum_bid", slice_h["sum_bid"])
        _dist_line("sum_no_ask", slice_h["sum_no_ask"])
        _dist_line("slack_ask (1-sum_ask, all signs)", slice_h["slack_ask"])
        _dist_line("slack_bid (sum_bid-1, all signs)", slice_h["slack_bid"])
        _dist_line("slack_no (5-sum_no_ask, all signs)", slice_h["slack_no"])
        buy = slice_h[slice_h["buy_violation"]]
        sell = slice_h[slice_h["sell_violation"]]
        no_v = slice_h[slice_h["no_violation"]]
        print(
            f"buy_viol frac={len(buy) / len(slice_h):.4f} n={len(buy)} "
            f"sell_viol frac={len(sell) / len(slice_h):.4f} n={len(sell)} "
            f"no_viol frac={len(no_v) / len(slice_h):.4f} n={len(no_v)}"
        )
        if len(buy):
            _dist_line("buy slack | viol", buy["slack_ask"])
            _dist_line("buy run candles | viol", buy["buy_run_n_candles"])
            _dist_line("buy run sec | viol", buy["buy_run_duration_sec"])
        if len(sell):
            _dist_line("sell slack | viol", sell["slack_bid"])
            _dist_line("sell run candles | viol", sell["sell_run_n_candles"])
            _dist_line("sell run sec | viol", sell["sell_run_duration_sec"])
        if len(no_v):
            _dist_line("no slack | viol", no_v["slack_no"])
            _dist_line("no run candles | viol", no_v["no_run_n_candles"])
            _dist_line("no run sec | viol", no_v["no_run_duration_sec"])
        _dist_line("sum_ask after fee", slice_h["sum_ask_after_fee"])
        _dist_line("sum_bid after fee", slice_h["sum_bid_after_fee"])
        _dist_line("sum_no_ask after fee", slice_h["sum_no_ask_after_fee"])
        band_h = slice_h[slice_h["all_in_band"]]
        if len(band_h):
            print(
                f"in-band complete n={len(band_h)} "
                f"buy_viol frac={float(band_h['buy_violation'].mean()):.4f} "
                f"sell_viol frac={float(band_h['sell_violation'].mean()):.4f} "
                f"no_viol frac={float(band_h['no_violation'].mean()):.4f}"
            )
        tails = slice_h[~slice_h["all_in_band"]]
        if len(tails):
            print(
                f"has-outside-band complete n={len(tails)} "
                f"buy_viol frac={float(tails['buy_violation'].mean()):.4f} "
                f"sell_viol frac={float(tails['sell_violation'].mean()):.4f} "
                f"no_viol frac={float(tails['no_violation'].mean()):.4f}"
            )
        pe_buy = slice_h[slice_h["penny_excluded_buy_violation"]]
        pe_sell = slice_h[slice_h["penny_excluded_sell_violation"]]
        from_penny = slice_h[slice_h["slack_from_penny_leg"]]
        persist = slice_h[
            (slice_h["buy_violation"] & (slice_h["buy_run_n_candles"].fillna(0) >= 2))
            | (slice_h["sell_violation"] & (slice_h["sell_run_n_candles"].fillna(0) >= 2))
        ]
        persist_survives = persist[~persist["slack_from_penny_leg"].fillna(False)]
        persist_from_penny = persist[persist["slack_from_penny_leg"].fillna(False)]
        print(
            f"penny-excl buy n={len(pe_buy)} sell n={len(pe_sell)} "
            f"slack_from_penny_leg n={len(from_penny)}"
        )
        print(
            f"persist>=2 union n={len(persist)} "
            f"still_violate_excl_penny n={len(persist_survives)} "
            f"slack_from_penny_leg n={len(persist_from_penny)}"
        )
    print("\n=== By season (pooled horizons, complete books) ===")
    for season in ("DJF", "MAM", "JJA", "SON"):
        slice_s = complete[complete["season"] == season]
        if slice_s.empty:
            continue
        print(
            f"{season} n={len(slice_s)} "
            f"buy_viol={float(slice_s['buy_violation'].mean()):.4f} "
            f"sell_viol={float(slice_s['sell_violation'].mean()):.4f} "
            f"no_viol={float(slice_s['no_violation'].mean()):.4f}"
        )
        _dist_line(f"{season} slack_ask all", slice_s["slack_ask"])
        _dist_line(f"{season} roc_buy", slice_s["roc_buy"])
        _dist_line(f"{season} roc_sell_gross", slice_s["roc_sell_gross"])
        _dist_line(f"{season} roc_sell_netted_1", slice_s["roc_sell_netted_1"])
        _dist_line(f"{season} roc_buy_no", slice_s["roc_buy_no"])
        _dist_line(f"{season} holding_hours", slice_s["holding_hours"])


def write_coverage_figure(coverage: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    in_win = coverage[coverage["in_window"]]
    if not in_win.empty:
        rates = in_win.groupby("horizon_h")["complete_book"].mean()
        axes[0].bar([str(h) for h in rates.index], rates.values)
    axes[0].set_xlabel("horizon hours before climate-day end")
    axes[0].set_ylabel("complete-book rate")
    axes[0].set_title("complete book among in-window (carry-forward)")
    seasons = ["DJF", "MAM", "JJA", "SON"]
    x = list(range(len(HORIZONS_H)))
    width = 0.18
    for i, season in enumerate(seasons):
        vals = []
        for horizon in HORIZONS_H:
            slice_hs = in_win[(in_win["horizon_h"] == horizon) & (in_win["season"] == season)]
            vals.append(float(slice_hs["complete_book"].mean()) if len(slice_hs) else 0.0)
        axes[1].bar([xi + i * width for xi in x], vals, width=width, label=season)
    axes[1].set_xticks([xi + 1.5 * width for xi in x], [str(h) for h in HORIZONS_H])
    axes[1].set_xlabel("horizon hours")
    axes[1].set_ylabel("complete-book rate")
    axes[1].set_title("complete book by season")
    axes[1].legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def write_violation_figures(frame: pd.DataFrame, out_dir: Path) -> list[Path]:
    complete = frame[frame["complete_book"]].copy()
    paths: list[Path] = []
    if complete.empty:
        return paths

    fig, ax = plt.subplots(figsize=(8, 4))
    data = [
        complete.loc[complete["horizon_h"] == h, "sum_ask"].dropna().to_numpy() for h in HORIZONS_H
    ]
    labels = [f"T-{h}h" for h in HORIZONS_H]
    ax.boxplot(data, tick_labels=labels)
    ax.axhline(1.0, color="black", linewidth=1)
    ax.set_ylabel("sum of YES asks")
    ax.set_title("sum-of-asks by horizon (complete books)")
    fig.tight_layout()
    path = out_dir / "relative_value_sum_asks_by_horizon.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, ax = plt.subplots(figsize=(8, 4))
    seasons = ["DJF", "MAM", "JJA", "SON"]
    x = list(range(len(seasons)))
    width = 0.25
    for i, col, label in (
        (0, "buy_violation", "buy set (sum ask < 1)"),
        (1, "sell_violation", "sell set (sum bid > 1)"),
        (2, "no_violation", "buy NO (sum no-ask < 5)"),
    ):
        vals = [
            float(complete.loc[complete["season"] == s, col].mean())
            if len(complete.loc[complete["season"] == s])
            else 0.0
            for s in seasons
        ]
        ax.bar([xi + i * width for xi in x], vals, width=width, label=label)
    ax.set_xticks([xi + width for xi in x], seasons)
    ax.set_ylabel("violation frequency")
    ax.set_title("violation frequency by season (complete books, pooled horizons)")
    ax.legend()
    fig.tight_layout()
    path = out_dir / "relative_value_violation_freq_by_season.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=False)
    panels = (
        ("roc_buy", "ROC buy-set (sum_ask capital)"),
        ("roc_sell_gross", "ROC sell-set gross (6-sum_bid)"),
        ("roc_sell_netted_1", "ROC sell-set netted $1 (UNVERIFIED)"),
    )
    for ax, (col, title) in zip(axes, panels, strict=True):
        values = complete[col].dropna().to_numpy()
        if len(values):
            ax.hist(values, bins=40)
        ax.set_title(title)
        ax.set_xlabel("per-trade ROC")
    fig.tight_layout()
    path = out_dir / "relative_value_roc_collateral.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)
    return paths


def _persist_for_day(
    climate_date: str,
    markets: list[dict[str, Any]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
    baskets: list[BasketSnapshot],
    candle_index: CandleIndex | None = None,
) -> dict[tuple[str, int], dict[str, tuple[int | None, float | None]]]:
    empty = {
        "buy": (None, None),
        "sell": (None, None),
        "no": (None, None),
    }
    out: dict[tuple[str, int], dict[str, tuple[int | None, float | None]]] = {
        (climate_date, basket.horizon_h): dict(empty) for basket in baskets
    }
    if not any(basket.complete_book for basket in baskets):
        return out
    settle_dt, _ = settlement_of_event(markets, climate_date)
    close_ts = int(settle_dt.timestamp())
    buy_events, sell_events, no_events = union_event_flags_all(
        markets, candles_by_ticker, climate_date, candle_index=candle_index
    )
    for basket in baskets:
        out[(climate_date, basket.horizon_h)] = {
            "buy": violation_run(buy_events, basket.snapshot_ts, close_ts),
            "sell": violation_run(sell_events, basket.snapshot_ts, close_ts),
            "no": violation_run(no_events, basket.snapshot_ts, close_ts),
        }
    return out


def build_baskets(
    universe: dict[str, list[dict[str, Any]]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
) -> list[BasketSnapshot]:
    baskets: list[BasketSnapshot] = []
    n_days = len(universe)
    for i, (climate_date, markets) in enumerate(sorted(universe.items()), start=1):
        if i % 100 == 0:
            logger.info("snapshots %s/%s", i, n_days)
        for horizon in HORIZONS_H:
            baskets.append(
                assemble_basket(
                    climate_date=climate_date,
                    horizon_h=horizon,
                    markets=markets,
                    candles_by_ticker=candles_by_ticker,
                )
            )
    return baskets


def run(config: dict[str, Any], out_dir: Path, phase: Phase) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    logger.info("loading markets")
    markets = load_markets(raw_dir)
    universe, other = six_bracket_universe(markets)
    other_counts: Counter[int] = Counter(other.values())
    tickers = {str(m.get("ticker") or "") for group in universe.values() for m in group}
    logger.info("universe_days=%s tickers=%s loading candles", len(universe), len(tickers))
    candles = load_extended_candles(raw_dir, tickers)
    logger.info("building horizon baskets")
    baskets = build_baskets(universe, candles)
    candle_index = build_candle_index(candles)
    coverage = pd.DataFrame([coverage_row(b) for b in baskets], columns=COVERAGE_COLUMNS)
    out_dir.mkdir(parents=True, exist_ok=True)
    cov_path = out_dir / "relative_value_coverage.csv"
    with cov_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTE_UNIVERSE}\n")
        handle.write(f"# {NOTE_COMPLETE}\n")
        handle.write("# This coverage file contains no bid/ask sums or violation magnitudes.\n")
    coverage.to_csv(cov_path, mode="a", index=False)
    cov_png = out_dir / "relative_value_complete_book_rate.png"
    write_coverage_figure(coverage, cov_png)
    _print_coverage_block(coverage, n_other=len(other), other_counts=other_counts)
    print(f"wrote {cov_path}")
    print(f"wrote {cov_png}")

    if phase == "coverage":
        print(NOTE_PHASE)
        return 0

    logger.info("violation persistence (union candle timeline per day)")
    persist_by_key: dict[tuple[str, int], dict[str, tuple[int | None, float | None]]] = {}
    by_day: dict[str, list[BasketSnapshot]] = {}
    for basket in baskets:
        by_day.setdefault(basket.climate_date, []).append(basket)
    n_days = len(by_day)
    for i, (climate_date, day_baskets) in enumerate(sorted(by_day.items()), start=1):
        if i % 50 == 0:
            logger.info("persistence %s/%s", i, n_days)
        persist_by_key.update(
            _persist_for_day(
                climate_date,
                universe[climate_date],
                candles,
                day_baskets,
                candle_index=candle_index,
            )
        )
    rows = [
        violation_row(
            basket,
            universe[basket.climate_date],
            persist_by_key.get((basket.climate_date, basket.horizon_h), {}),
        )
        for basket in baskets
    ]
    frame = pd.DataFrame(rows)
    csv_path = out_dir / "relative_value_census.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTE_UNIVERSE}\n")
        handle.write(f"# {NOTE_NO_BASKET}\n")
        handle.write(f"# {NOTE_DEPTH}\n")
        handle.write(f"# {NOTE_NETTING}\n")
        handle.write(f"# {NOTE_FEE}\n")
        handle.write(
            "# annualized ROC = roc_per_trade * (8760 / holding_hours), simple not compounded.\n"
        )
    frame.to_csv(csv_path, mode="a", index=False)
    pngs = write_violation_figures(frame, out_dir)
    print_violations(frame)
    print(f"wrote {csv_path}")
    for path in pngs:
        print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="S2 relative-value census (distributions only)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    parser.add_argument(
        "--phase",
        choices=("coverage", "violations", "all"),
        default="coverage",
        help="coverage prints complete-book rates and stops; violations adds executable sums",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(config, args.out_dir, args.phase)


if __name__ == "__main__":
    raise SystemExit(main())
