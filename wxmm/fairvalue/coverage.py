"""Trade-derived two-sided coverage on an hourly hours-to-close grid.

Run this before any v0 fit. A model trained on a narrow liquidity-selected
slice is the design-rule-4 regime leak: report coverage and stop.

Allowed to assume
    ``created_time`` is the print's available_at. Mapping is already clean.

Must never
    Impute a missing side as two-sided. Fit when the two-sided share is
    below the pre-registered floor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Mapping, Sequence

from wxmm.analysis.maker_taker import season_of
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.core.errors import CoverageFloorRefused
from wxmm.fairvalue.anchor_trades import (
    ObservedMapping,
    TradeImpliedBook,
    YesSpacePrint,
    apply_print,
    implied_book_from_trades,
    stamp_staleness,
    yes_space_print,
)
from wxmm.fairvalue.ladder import parse_kalshi_bracket
from wxmm.settlement.eras import kalshi_last_trading_close_utc

COVERAGE_HOURS: tuple[int, ...] = tuple(range(0, 25))
DEFAULT_COVERAGE_FLOOR = 0.15
STALE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("lt_5m", 300.0),
    ("lt_1h", 3600.0),
    ("lt_6h", 21600.0),
    ("ge_6h", float("inf")),
)


@dataclass(frozen=True, slots=True)
class CoverageSlice:
    key: str
    n_grid: int
    n_two_sided_uncrossed: int
    n_one_sided: int
    n_missing: int
    n_crossed_resolved: int
    share: float


@dataclass(frozen=True, slots=True)
class TradeAnchorCoverage:
    n_grid: int
    n_two_sided_uncrossed: int
    n_one_sided: int
    n_missing: int
    n_crossed_resolved: int
    two_sided_share: float
    crossed_rate: float
    floor: float
    by_season: tuple[CoverageSlice, ...]
    by_hours_to_close: tuple[CoverageSlice, ...]
    by_bracket_role: tuple[CoverageSlice, ...]
    bid_staleness_seconds: tuple[float, ...]
    ask_staleness_seconds: tuple[float, ...]
    is_strategy_pnl: Literal[False] = False

    def as_dict(self) -> dict[str, object]:
        return {
            "n_grid": self.n_grid,
            "n_two_sided_uncrossed": self.n_two_sided_uncrossed,
            "n_one_sided": self.n_one_sided,
            "n_missing": self.n_missing,
            "n_crossed_resolved": self.n_crossed_resolved,
            "two_sided_share": self.two_sided_share,
            "crossed_rate": self.crossed_rate,
            "floor": self.floor,
            "by_season": [asdict(slice_) for slice_ in self.by_season],
            "by_hours_to_close": [asdict(slice_) for slice_ in self.by_hours_to_close],
            "by_bracket_role": [asdict(slice_) for slice_ in self.by_bracket_role],
            "n_bid_staleness": len(self.bid_staleness_seconds),
            "n_ask_staleness": len(self.ask_staleness_seconds),
            "is_strategy_pnl": False,
        }


def _role(ticker: str) -> str:
    parsed = parse_kalshi_bracket(ticker)
    return parsed.role if parsed is not None else "unknown"


def _slice(rows: Sequence[tuple[str, str, str, str]], key: str) -> CoverageSlice:
    n = len(rows)
    two = sum(1 for _s, _h, _r, state in rows if state == "two_sided")
    one = sum(1 for _s, _h, _r, state in rows if state == "one_sided")
    missing = sum(1 for _s, _h, _r, state in rows if state == "missing")
    crossed = sum(1 for _s, _h, _r, state in rows if state == "crossed")
    return CoverageSlice(
        key=key,
        n_grid=n,
        n_two_sided_uncrossed=two,
        n_one_sided=one,
        n_missing=missing,
        n_crossed_resolved=crossed,
        share=(two / n) if n else 0.0,
    )


def coverage_grid_times(climate_day: date) -> list[tuple[int, datetime]]:
    close = kalshi_last_trading_close_utc(climate_day)
    return [(hour, close - timedelta(hours=hour)) for hour in COVERAGE_HOURS]


def _state_of(book: TradeImpliedBook) -> str:
    if book.mid is not None:
        return "two_sided"
    if book.bid is None and book.ask is None:
        return "missing"
    if book.crossed_invalidations > 0:
        return "crossed"
    return "one_sided"


def _scan_ticker(
    trades: Sequence[RawTrade],
    *,
    as_of_grid: list[tuple[int, datetime]],
) -> list[tuple[int, TradeImpliedBook]]:
    if not as_of_grid:
        return []
    ordered = sorted(trades, key=lambda t: (t.created_time, t.trade_id))
    book = implied_book_from_trades([], as_of=as_of_grid[0][1])
    i = 0
    snapshots: list[tuple[int, TradeImpliedBook]] = []
    for hour, as_of in as_of_grid:
        while i < len(ordered) and ordered[i].created_time <= as_of:
            trade = ordered[i]
            i += 1
            if trade.is_block_trade:
                dummy = YesSpacePrint(
                    direction=1,
                    yes_price=trade.yes_price,
                    created_time=trade.created_time,
                    count=trade.count,
                    ticker=trade.ticker,
                )
                book = apply_print(book, dummy, as_of=as_of, is_block=True)
            else:
                book = apply_print(book, yes_space_print(trade), as_of=as_of, is_block=False)
        book = stamp_staleness(book, as_of)
        snapshots.append((hour, book))
    return snapshots


def trade_anchor_coverage(
    trades: Sequence[RawTrade],
    *,
    mapping: ObservedMapping,
    floor: float = DEFAULT_COVERAGE_FLOOR,
    universe: Sequence[tuple[str, date]] | None = None,
    roles: Mapping[str, str] | None = None,
) -> TradeAnchorCoverage:
    _ = mapping  # required: caller must have passed a clean bijection
    by_ticker: dict[tuple[str, date], list[RawTrade]] = {}
    for trade in trades:
        by_ticker.setdefault((trade.ticker, trade.climate_day), []).append(trade)
    pairs = sorted(universe) if universe is not None else sorted(by_ticker)
    rows: list[tuple[str, str, str, str]] = []
    bid_stale: list[float] = []
    ask_stale: list[float] = []
    for ticker, climate in pairs:
        season = season_of(climate)
        role = (roles or {}).get(ticker) or _role(ticker)
        grid = sorted(coverage_grid_times(climate), key=lambda item: item[1])
        snaps = _scan_ticker(by_ticker.get((ticker, climate), ()), as_of_grid=grid)
        for hour, book in snaps:
            state = _state_of(book)
            if state == "two_sided":
                if book.bid_staleness is not None:
                    bid_stale.append(book.bid_staleness.total_seconds())
                if book.ask_staleness is not None:
                    ask_stale.append(book.ask_staleness.total_seconds())
            rows.append((season, f"T-{hour}h", role, state))

    n = len(rows)
    two = sum(1 for _a, _b, _c, state in rows if state == "two_sided")
    one = sum(1 for _a, _b, _c, state in rows if state == "one_sided")
    missing = sum(1 for _a, _b, _c, state in rows if state == "missing")
    crossed = sum(1 for _a, _b, _c, state in rows if state == "crossed")
    seasons = tuple(
        _slice([row for row in rows if row[0] == season], season)
        for season in ("DJF", "MAM", "JJA", "SON")
        if any(row[0] == season for row in rows)
    )
    hours = tuple(
        _slice([row for row in rows if row[1] == label], label)
        for label in (f"T-{hour}h" for hour in COVERAGE_HOURS)
        if any(row[1] == f"T-{hour}h" for row in rows)
    )
    role_slices = tuple(
        _slice([row for row in rows if row[2] == role], role)
        for role in sorted({row[2] for row in rows})
    )
    return TradeAnchorCoverage(
        n_grid=n,
        n_two_sided_uncrossed=two,
        n_one_sided=one,
        n_missing=missing,
        n_crossed_resolved=crossed,
        two_sided_share=(two / n) if n else 0.0,
        crossed_rate=(crossed / n) if n else 0.0,
        floor=floor,
        by_season=seasons,
        by_hours_to_close=hours,
        by_bracket_role=role_slices,
        bid_staleness_seconds=tuple(bid_stale),
        ask_staleness_seconds=tuple(ask_stale),
    )


def refuse_unless_coverage(
    coverage: TradeAnchorCoverage,
    *,
    floor: float | None = None,
) -> TradeAnchorCoverage:
    limit = coverage.floor if floor is None else floor
    if coverage.n_grid == 0 or coverage.two_sided_share < limit:
        raise CoverageFloorRefused(
            f"two_sided_share={coverage.two_sided_share:.4f} below floor {limit:.4f}; "
            "report coverage, do not fit",
            share=coverage.two_sided_share,
            floor=limit,
        )
    return coverage


def mm_program_era(climate_day: date) -> str:
    """Kalshi MM-program slices. Post-2026-03-12 is unverified."""
    if climate_day < date(2024, 3, 11):
        return "pre_2024-03-11"
    if climate_day <= date(2026, 3, 11):
        return "program_2024-03-11_to_2026-03-11"
    return "post_2026-03-12_PROGRAM_STATUS_UNVERIFIED"


def hours_to_close_bucket(hours: float) -> str:
    if hours >= 24:
        return "T-24h+"
    if hours >= 12:
        return "T-12h"
    if hours >= 6:
        return "T-6h"
    if hours >= 3:
        return "T-3h"
    if hours >= 1:
        return "T-1h"
    return "T-lt1h"


def staleness_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    for name, cap in STALE_BUCKETS:
        if seconds < cap:
            return name
    return "ge_6h"
