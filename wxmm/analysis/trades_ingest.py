"""Paginated pull of Kalshi trades: historical + live, merged at the cutoff.

Allowed to assume
    ``GET /historical/cutoff`` is the partition. Trades filled before the
    cutoff are on ``GET /historical/trades``; later fills are on
    ``GET /markets/trades``. Both carry canonical taker fields.

Must never
    Assume the cutoff. Read an order book or candle. Read the deprecated
    aggressor field. Invent ``yes_price + no_price = 1``. Skip the
    complement assertion.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

import duckdb
import polars as pl

from wxmm.core.errors import PriceComplementError
from wxmm.core.utc import require_utc
from wxmm.settlement.eras import (
    KALSHI_LAST_TRADING_CIVIL_ET_THRU,
    kalshi_rule_for,
)

PRICE_COMPLEMENT_TOLERANCE = Decimal("0.0001")
SIX_BRACKET_ERA_START = date(2022, 12, 11)
TICKER_RE = re.compile(
    r"^(?:KX)?HIGHNY-(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|"
    r"OCT|NOV|DEC)(?P<dd>\d{2})-",
    re.IGNORECASE,
)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

BookSide = Literal["bid", "ask"]
OutcomeSide = Literal["yes", "no"]
LadderRegime = Literal["six_bracket", "pre_six_bracket"]
CloseTimeConvention = Literal["civil_et_1159", "lst_1159"]


class TradesTransport(Protocol):
    """Injected HTTP. Production wraps the Kalshi client; tests inject fakes."""

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HistoricalCutoff:
    market_settled_ts: int
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RawTrade:
    trade_id: str
    ticker: str
    count: Decimal
    yes_price: Decimal
    no_price: Decimal
    taker_outcome_side: OutcomeSide
    taker_book_side: BookSide
    created_time: datetime
    is_block_trade: bool
    source_endpoint: Literal["historical", "live"]
    climate_day: date
    ladder_regime: LadderRegime
    settlement_rule_id: str
    close_time_convention: CloseTimeConvention


@dataclass(frozen=True, slots=True)
class MergeReport:
    n_historical: int
    n_live: int
    n_merged: int
    n_duplicate_ids: int
    duplicate_ids: tuple[str, ...]
    n_boundary_overlap: int
    gap_note: str


def parse_climate_day(ticker: str) -> date:
    match = TICKER_RE.match(ticker)
    if match is None:
        raise ValueError(f"not a KXHIGHNY/HIGHNY ticker: {ticker!r}")
    year = 2000 + int(match.group("yy"))
    month = _MONTHS[match.group("mon").upper()]
    day = int(match.group("dd"))
    return date(year, month, day)


def ladder_regime_for(climate_day: date) -> LadderRegime:
    if climate_day >= SIX_BRACKET_ERA_START:
        return "six_bracket"
    return "pre_six_bracket"


def close_time_convention_for(climate_day: date) -> CloseTimeConvention:
    if climate_day <= KALSHI_LAST_TRADING_CIVIL_ET_THRU:
        return "civil_et_1159"
    return "lst_1159"


def assert_price_complement(yes_price: Decimal, no_price: Decimal, *, trade_id: str) -> None:
    total = yes_price + no_price
    if abs(total - Decimal("1")) > PRICE_COMPLEMENT_TOLERANCE:
        raise PriceComplementError(
            f"trade {trade_id}: yes_price={yes_price} + no_price={no_price} = {total} "
            f"(docs example 0.56+0.56 is placeholder text; refuse rather than invert P&L)"
        )


def _parse_created_time(raw: str) -> datetime:
    text = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return require_utc(parsed)


def parse_trade(
    payload: dict[str, Any],
    *,
    source_endpoint: Literal["historical", "live"],
) -> RawTrade:
    if "taker_outcome_side" not in payload or "taker_book_side" not in payload:
        raise ValueError(
            "canonical taker_outcome_side and taker_book_side are required "
            "(deprecated aggressor field is not a substitute)"
        )
    outcome = payload["taker_outcome_side"]
    book = payload["taker_book_side"]
    if outcome not in {"yes", "no"}:
        raise ValueError(f"taker_outcome_side must be yes|no, got {outcome!r}")
    if book not in {"bid", "ask"}:
        raise ValueError(f"taker_book_side must be bid|ask, got {book!r}")
    yes_price = Decimal(str(payload["yes_price_dollars"]))
    no_price = Decimal(str(payload["no_price_dollars"]))
    trade_id = str(payload["trade_id"])
    assert_price_complement(yes_price, no_price, trade_id=trade_id)
    ticker = str(payload["ticker"])
    climate = parse_climate_day(ticker)
    created = _parse_created_time(str(payload["created_time"]))
    return RawTrade(
        trade_id=trade_id,
        ticker=ticker,
        count=Decimal(str(payload["count_fp"])),
        yes_price=yes_price,
        no_price=no_price,
        taker_outcome_side=outcome,
        taker_book_side=book,
        created_time=created,
        is_block_trade=bool(payload.get("is_block_trade", False)),
        source_endpoint=source_endpoint,
        climate_day=climate,
        ladder_regime=ladder_regime_for(climate),
        settlement_rule_id=kalshi_rule_for(climate).rule_id,
        close_time_convention=close_time_convention_for(climate),
    )


def parse_cutoff(payload: dict[str, Any]) -> HistoricalCutoff:
    raw: Any = payload.get("market_settled_ts")
    if raw is None:
        raw = payload.get("marketSettledTs")
    if raw is None:
        nested = payload.get("cutoff") or payload.get("cutoffs") or {}
        if isinstance(nested, dict):
            raw = nested.get("market_settled_ts")
    if raw is None:
        raise ValueError("GET /historical/cutoff returned no market_settled_ts")
    if isinstance(raw, (int, float)):
        ts = int(raw)
    else:
        text = str(raw).replace("Z", "+00:00")
        ts = int(datetime.fromisoformat(text).timestamp())
    return HistoricalCutoff(market_settled_ts=ts, raw=payload)


def fetch_cutoff(transport: TradesTransport) -> HistoricalCutoff:
    """Read the cutoff before any backfill. Do not assume it."""
    return parse_cutoff(transport.get_json("/historical/cutoff"))


def paginate_trades(
    transport: TradesTransport,
    *,
    path: str,
    source_endpoint: Literal["historical", "live"],
    ticker: str | None = None,
    min_ts: int | None = None,
    max_ts: int | None = None,
    limit: int = 1000,
    is_block_trade: bool | None = None,
) -> list[RawTrade]:
    cursor: str | None = None
    out: list[RawTrade] = []
    while True:
        params: dict[str, Any] = {"limit": limit}
        if ticker is not None:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if is_block_trade is not None:
            params["is_block_trade"] = is_block_trade
        if cursor:
            params["cursor"] = cursor
        body = transport.get_json(path, params)
        rows = body.get("trades") or []
        if not isinstance(rows, list):
            raise ValueError(f"{path} trades field is not a list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{path} trade row is not an object")
            out.append(parse_trade(row, source_endpoint=source_endpoint))
        nxt = body.get("cursor")
        if not nxt or not rows:
            break
        cursor = str(nxt)
    return out


def pull_ticker(
    transport: TradesTransport,
    ticker: str,
    cutoff: HistoricalCutoff,
    *,
    limit: int = 1000,
) -> tuple[list[RawTrade], list[RawTrade]]:
    historical = paginate_trades(
        transport,
        path="/historical/trades",
        source_endpoint="historical",
        ticker=ticker,
        max_ts=cutoff.market_settled_ts,
        limit=limit,
    )
    live = paginate_trades(
        transport,
        path="/markets/trades",
        source_endpoint="live",
        ticker=ticker,
        min_ts=cutoff.market_settled_ts,
        limit=limit,
    )
    return historical, live


def merge_trades(
    historical: list[RawTrade],
    live: list[RawTrade],
    cutoff: HistoricalCutoff,
) -> tuple[list[RawTrade], MergeReport]:
    """Merge live + historical; surface duplicates and holes at the cutoff."""
    by_id: dict[str, RawTrade] = {}
    duplicates: list[str] = []
    for trade in historical + live:
        existing = by_id.get(trade.trade_id)
        if existing is None:
            by_id[trade.trade_id] = trade
            continue
        duplicates.append(trade.trade_id)
    merged = sorted(by_id.values(), key=lambda t: (t.created_time, t.trade_id))
    cutoff_dt = datetime.fromtimestamp(cutoff.market_settled_ts, tz=timezone.utc)
    hist_after = [t for t in historical if t.created_time >= cutoff_dt]
    live_before = [t for t in live if t.created_time < cutoff_dt]
    overlap = len(hist_after) + len(live_before)
    gap_note = ""
    if historical and live:
        last_h = max(t.created_time for t in historical)
        first_l = min(t.created_time for t in live)
        if first_l > last_h:
            gap_note = (
                f"time hole at cutoff: last_historical={last_h.isoformat()} "
                f"first_live={first_l.isoformat()} cutoff={cutoff_dt.isoformat()}"
            )
    report = MergeReport(
        n_historical=len(historical),
        n_live=len(live),
        n_merged=len(merged),
        n_duplicate_ids=len(set(duplicates)),
        duplicate_ids=tuple(sorted(set(duplicates))),
        n_boundary_overlap=overlap,
        gap_note=gap_note,
    )
    return merged, report


def boundary_audit_duckdb(
    historical: list[RawTrade],
    live: list[RawTrade],
) -> int:
    """Duplicate ``trade_id`` count across the two endpoint dumps (duckdb)."""
    if not historical or not live:
        return 0
    hist_df = pl.DataFrame({"trade_id": [t.trade_id for t in historical]})
    live_df = pl.DataFrame({"trade_id": [t.trade_id for t in live]})
    con = duckdb.connect(":memory:")
    try:
        con.register("hist", hist_df)
        con.register("live", live_df)
        row = con.execute(
            "SELECT COUNT(*) FROM hist h JOIN live l ON h.trade_id = l.trade_id"
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def trades_to_parquet(trades: list[RawTrade], path: Any) -> None:
    records = []
    for trade in trades:
        row = asdict(trade)
        row["created_time"] = trade.created_time.isoformat()
        row["climate_day"] = trade.climate_day.isoformat()
        row["count"] = str(trade.count)
        row["yes_price"] = str(trade.yes_price)
        row["no_price"] = str(trade.no_price)
        records.append(row)
    pl.DataFrame(records).write_parquet(path)
