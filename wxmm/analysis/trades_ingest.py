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

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import duckdb
import polars as pl

from wxmm.analysis.raw_store import CheckpointStore, EndpointCursor, TickerCheckpoint
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
    trades_created_ts: int

    @property
    def trade_partition_ts(self) -> int:
        """Trades are partitioned by fill time, not market settlement time."""
        return self.trades_created_ts


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


@dataclass
class ComplementTally:
    """Count complement violations; never a reason to drop a ticker."""

    n_checked: int = 0
    n_violations: int = 0
    worst_deviation: Decimal = Decimal("0")
    worst_trade_id: str = ""
    worst_ticker: str = ""
    violating_ids: set[str] = field(default_factory=set)
    samples: list[dict[str, str]] = field(default_factory=list)

    def observe(self, yes_price: Decimal, no_price: Decimal, *, trade_id: str, ticker: str) -> bool:
        self.n_checked += 1
        deviation = abs(yes_price + no_price - Decimal("1"))
        if deviation <= PRICE_COMPLEMENT_TOLERANCE:
            return False
        self.n_violations += 1
        self.violating_ids.add(trade_id)
        if deviation > self.worst_deviation:
            self.worst_deviation = deviation
            self.worst_trade_id = trade_id
            self.worst_ticker = ticker
        if len(self.samples) < 50:
            self.samples.append(
                {
                    "trade_id": trade_id,
                    "ticker": ticker,
                    "yes_price": str(yes_price),
                    "no_price": str(no_price),
                    "deviation": str(deviation),
                }
            )
        return True

    def merge_from(self, other: ComplementTally) -> None:
        self.n_checked += other.n_checked
        self.n_violations += other.n_violations
        self.violating_ids.update(other.violating_ids)
        if other.worst_deviation > self.worst_deviation:
            self.worst_deviation = other.worst_deviation
            self.worst_trade_id = other.worst_trade_id
            self.worst_ticker = other.worst_ticker
        remaining = 50 - len(self.samples)
        if remaining > 0:
            self.samples.extend(other.samples[:remaining])

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_checked": self.n_checked,
            "n_violations": self.n_violations,
            "worst_deviation": str(self.worst_deviation),
            "worst_trade_id": self.worst_trade_id,
            "worst_ticker": self.worst_ticker,
            "samples": list(self.samples),
        }


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
    """Strict helper. Unit tests that expect a raise call this, not ingest."""
    total = yes_price + no_price
    if abs(total - Decimal("1")) > PRICE_COMPLEMENT_TOLERANCE:
        raise PriceComplementError(
            f"trade {trade_id}: yes_price={yes_price} + no_price={no_price} = {total} "
            f"(docs example 0.56+0.56 is placeholder text; refuse rather than invert P&L)"
        )


def complement_deviation(yes_price: Decimal, no_price: Decimal) -> Decimal:
    return abs(yes_price + no_price - Decimal("1"))


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
    strict_complement: bool = True,
    complement_tally: ComplementTally | None = None,
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
    ticker = str(payload["ticker"])
    if complement_tally is not None:
        complement_tally.observe(yes_price, no_price, trade_id=trade_id, ticker=ticker)
    if strict_complement:
        assert_price_complement(yes_price, no_price, trade_id=trade_id)
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


def _cutoff_field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    nested = payload.get("cutoff") or payload.get("cutoffs") or {}
    if isinstance(nested, dict):
        for name in names:
            if nested.get(name) is not None:
                return nested[name]
    return None


def _as_unix_ts(raw: Any, *, field: str) -> int:
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError as exc:
        raise ValueError(f"GET /historical/cutoff {field} is not a timestamp: {raw!r}") from exc


def parse_cutoff(payload: dict[str, Any]) -> HistoricalCutoff:
    settled_raw = _cutoff_field(payload, "market_settled_ts", "marketSettledTs")
    if settled_raw is None:
        raise ValueError("GET /historical/cutoff returned no market_settled_ts")
    market_settled_ts = _as_unix_ts(settled_raw, field="market_settled_ts")
    trades_raw = _cutoff_field(payload, "trades_created_ts", "tradesCreatedTs")
    trades_created_ts = (
        _as_unix_ts(trades_raw, field="trades_created_ts")
        if trades_raw is not None
        else market_settled_ts
    )
    return HistoricalCutoff(
        market_settled_ts=market_settled_ts,
        trades_created_ts=trades_created_ts,
        raw=payload,
    )


def fetch_cutoff(transport: TradesTransport) -> HistoricalCutoff:
    """Read the cutoff before any backfill. Do not assume it."""
    return parse_cutoff(transport.get_json("/historical/cutoff"))


def _append_inflight(path: Path, trades: list[RawTrade]) -> None:
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for trade in trades:
            handle.write(json.dumps(raw_trade_to_record(trade), sort_keys=True) + "\n")


def load_inflight_jsonl(path: Path) -> list[RawTrade]:
    if not path.exists():
        return []
    out: list[RawTrade] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"inflight row is not an object: {path}")
        out.append(record_to_raw_trade(row, strict_complement=False))
    return out


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
    start_cursor: str | None = None,
    strict_complement: bool = True,
    complement_tally: ComplementTally | None = None,
    checkpoint: EndpointCursor | None = None,
    checkpoint_store: CheckpointStore | None = None,
    checkpoint_owner: TickerCheckpoint | None = None,
    on_page: Callable[[int, list[RawTrade]], None] | None = None,
) -> list[RawTrade]:
    cursor: str | None = start_cursor
    if checkpoint is not None and checkpoint.cursor and not checkpoint.done:
        cursor = checkpoint.cursor
    out: list[RawTrade] = []
    inflight_path: Path | None = None
    if checkpoint_store is not None and ticker is not None:
        inflight_path = checkpoint_store.inflight_path(ticker, source_endpoint)
        seen = {t.trade_id: t for t in load_inflight_jsonl(inflight_path)}
        out.extend(seen.values())
        if checkpoint is not None and checkpoint.done:
            return out
    pages = 0
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
        page_trades: list[RawTrade] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{path} trade row is not an object")
            page_trades.append(
                parse_trade(
                    row,
                    source_endpoint=source_endpoint,
                    strict_complement=strict_complement,
                    complement_tally=complement_tally,
                )
            )
        existing_ids = {trade.trade_id for trade in out}
        new_trades = [trade for trade in page_trades if trade.trade_id not in existing_ids]
        out.extend(new_trades)
        if inflight_path is not None:
            _append_inflight(inflight_path, new_trades)
        nxt = body.get("cursor")
        done = not nxt or not rows
        if checkpoint is not None:
            checkpoint.cursor = None if done else str(nxt)
            if page_trades:
                checkpoint.last_created_time = page_trades[-1].created_time.isoformat()
            checkpoint.n_trades = len(out)
            checkpoint.done = done
            if checkpoint_store is not None and checkpoint_owner is not None:
                checkpoint_store.save(checkpoint_owner)
        pages += 1
        if on_page is not None:
            on_page(pages, page_trades)
        if done:
            break
        cursor = str(nxt)
    return out


def pull_ticker(
    transport: TradesTransport,
    ticker: str,
    cutoff: HistoricalCutoff,
    *,
    limit: int = 1000,
    strict_complement: bool = True,
    complement_tally: ComplementTally | None = None,
    checkpoints: CheckpointStore | None = None,
    on_page: Callable[[int, list[RawTrade]], None] | None = None,
) -> tuple[list[RawTrade], list[RawTrade]]:
    owner = checkpoints.load(ticker) if checkpoints is not None else None
    if owner is None:
        owner = TickerCheckpoint.fresh(ticker)
    partition = cutoff.trade_partition_ts
    historical = paginate_trades(
        transport,
        path="/historical/trades",
        source_endpoint="historical",
        ticker=ticker,
        max_ts=partition,
        limit=limit,
        strict_complement=strict_complement,
        complement_tally=complement_tally,
        checkpoint=owner.historical,
        checkpoint_store=checkpoints,
        checkpoint_owner=owner,
        on_page=on_page,
    )
    live = paginate_trades(
        transport,
        path="/markets/trades",
        source_endpoint="live",
        ticker=ticker,
        min_ts=partition,
        limit=limit,
        strict_complement=strict_complement,
        complement_tally=complement_tally,
        checkpoint=owner.live,
        checkpoint_store=checkpoints,
        checkpoint_owner=owner,
        on_page=on_page,
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
    cutoff_dt = datetime.fromtimestamp(cutoff.trade_partition_ts, tz=timezone.utc)
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


def raw_trade_to_record(trade: RawTrade) -> dict[str, Any]:
    row = asdict(trade)
    row["created_time"] = trade.created_time.isoformat()
    row["climate_day"] = trade.climate_day.isoformat()
    row["count"] = str(trade.count)
    row["yes_price"] = str(trade.yes_price)
    row["no_price"] = str(trade.no_price)
    return row


def record_to_raw_trade(
    row: dict[str, Any],
    *,
    strict_complement: bool = True,
    complement_tally: ComplementTally | None = None,
) -> RawTrade:
    yes_price = Decimal(str(row["yes_price"]))
    no_price = Decimal(str(row["no_price"]))
    trade_id = str(row["trade_id"])
    ticker = str(row["ticker"])
    if complement_tally is not None:
        complement_tally.observe(yes_price, no_price, trade_id=trade_id, ticker=ticker)
    if strict_complement:
        assert_price_complement(yes_price, no_price, trade_id=trade_id)
    return RawTrade(
        trade_id=trade_id,
        ticker=ticker,
        count=Decimal(str(row["count"])),
        yes_price=yes_price,
        no_price=no_price,
        taker_outcome_side=row["taker_outcome_side"],
        taker_book_side=row["taker_book_side"],
        created_time=_parse_created_time(str(row["created_time"])),
        is_block_trade=bool(row["is_block_trade"]),
        source_endpoint=row["source_endpoint"],
        climate_day=date.fromisoformat(str(row["climate_day"])),
        ladder_regime=row["ladder_regime"],
        settlement_rule_id=str(row["settlement_rule_id"]),
        close_time_convention=row["close_time_convention"],
    )


def parquet_shard_paths(path: Any) -> list[Path]:
    """Monthly ``climate_month=*.parquet`` shards, else a single file, else ``_tickers``."""
    target = Path(path)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    monthly = sorted(p for p in target.glob("climate_month=*.parquet") if p.is_file())
    if monthly:
        return monthly
    top = sorted(p for p in target.glob("*.parquet") if p.is_file())
    if top:
        return top
    nested = target / "_tickers"
    if nested.is_dir():
        return sorted(nested.glob("*.parquet"))
    return []


def read_trades_parquet(
    path: Any,
    *,
    strict_complement: bool = True,
    complement_tally: ComplementTally | None = None,
) -> list[RawTrade]:
    """Round-trip of ``trades_to_parquet``. Accepts a file or a shard directory."""
    target = Path(path)
    if target.is_dir():
        files = sorted(p for p in target.glob("*.parquet") if p.is_file())
        if not files:
            nested = target / "_tickers"
            files = sorted(nested.glob("*.parquet")) if nested.is_dir() else []
    else:
        files = [target]
    out: list[RawTrade] = []
    for file in files:
        frame = pl.read_parquet(file)
        for row in frame.iter_rows(named=True):
            out.append(
                record_to_raw_trade(
                    row,
                    strict_complement=strict_complement,
                    complement_tally=complement_tally,
                )
            )
    out.sort(key=lambda t: (t.created_time, t.trade_id))
    return out


def trades_to_parquet(trades: list[RawTrade], path: Any) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    records = [raw_trade_to_record(trade) for trade in trades]
    pl.DataFrame(records).write_parquet(dest)
