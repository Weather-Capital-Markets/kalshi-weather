"""Deterministic event replay. No async. Clock is the only now."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from wxmm.backtest.clock import SimClock
from wxmm.backtest.costs import fill_cost
from wxmm.backtest.fills import Fill, last_in_queue_fill
from wxmm.backtest.guards import wall_clock_guard
from wxmm.backtest.ledger import (
    BacktestResult,
    HeldOutUnlock,
    Ledger,
    config_hash,
    git_sha,
    refuse_if_held_out_locked,
    refuse_unless_preregistered,
)
from wxmm.core.money import Money
from wxmm.core.types import (
    AsOfRecord,
    BookSnapshot,
    ClockBoundStore,
    InMemoryAsOfStore,
    Order,
    ReadContext,
    Trade,
)
from wxmm.core.utc import require_utc
from wxmm.data.quality import CoverageRow, build_coverage_report, row_from_book
from wxmm.data.vintage import VintageStore, snapshot_id_for
from wxmm.strategy.protocol import Strategy
from wxmm.venues.base import Venue
from wxmm.venues.kalshi.fees import FeeRounding


@dataclass(frozen=True, slots=True)
class MarketEvent:
    ts: datetime
    kind: str
    market_id: str
    payload: object
    climate_day: str


def run(
    *,
    config: Mapping[str, Any],
    prereg_dir: Path,
    strategy: Strategy,
    venue: Venue,
    events: Sequence[MarketEvent],
    books_by_event: Mapping[int, BookSnapshot | None] | None = None,
    trades_by_market: Mapping[str, tuple[Trade, ...]] | None = None,
    vintage: VintageStore | None = None,
    coverage_rows: Sequence[CoverageRow] | None = None,
    held_out_unlock: HeldOutUnlock | None = None,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
    inner_store: InMemoryAsOfStore | None = None,
) -> BacktestResult:
    """Run a backtest. Refuses config hashes not in ``prereg/``.

    ``coverage_rows`` must be supplied (or derived from ``books_by_event``);
    a result without a coverage report is invalid.
    """
    registered = refuse_unless_preregistered(config, prereg_dir)
    ledger = Ledger()
    climate_days = tuple(date.fromisoformat(ev.climate_day) for ev in events)
    refuse_if_held_out_locked(
        config,
        registered,
        climate_days=climate_days,
        unlock=held_out_unlock,
        ledger=ledger,
    )

    ordered = sorted(events, key=lambda ev: (require_utc(ev.ts), ev.kind, ev.market_id))
    if not ordered:
        coverage = build_coverage_report(list(coverage_rows or ()))
        snap = vintage.snapshot().snapshot_id if vintage is not None else snapshot_id_for(())
        return BacktestResult(
            config_hash=config_hash(config),
            prereg_id=str(config.get("prereg_id", registered.get("prereg_id", ""))),
            git_sha=git_sha(),
            snapshot_id=snap,
            seed=int(config.get("seed", 0)),
            universe=tuple(str(item) for item in config.get("universe", ())),
            fills=(),
            pnl=Money.zero(),
            coverage=coverage,
            extra={"ledger": [entry.payload for entry in ledger.entries]},
        )

    clock = SimClock(ordered[0].ts)
    store = ClockBoundStore(inner_store or InMemoryAsOfStore(), clock)
    fills: list[Fill] = []
    pnl = Money.zero()
    last_book: dict[str, BookSnapshot] = {}
    trades_by_market = trades_by_market or {}

    for index, event in enumerate(ordered):
        clock.advance_to(event.ts)
        if event.kind == "book" and isinstance(event.payload, BookSnapshot):
            last_book[event.market_id] = event.payload
            store.put(
                AsOfRecord(
                    key=f"book:{event.market_id}",
                    payload=event.payload,
                    valid_at=event.ts,
                    available_at=event.ts,
                    source="replay",
                    ingest_run_id="replay",
                )
            )
        ctx = ReadContext(clock=clock, store=store)
        with wall_clock_guard():
            proposed: Sequence[Order] = tuple(strategy.on_event(ctx))
        book = last_book.get(event.market_id)
        if books_by_event is not None and index in books_by_event:
            maybe = books_by_event[index]
            if maybe is not None:
                book = maybe
        for order in proposed:
            if book is None:
                continue
            fill = last_in_queue_fill(order, book, trades_by_market.get(order.market_id, ()))
            fills.append(fill)
            if fill.status in {"filled", "partial"} and fill.filled_qty:
                pnl = pnl - fill_cost(venue, order, rounding)

    if coverage_rows is not None:
        coverage = build_coverage_report(list(coverage_rows))
    else:
        rows = [
            row_from_book(
                venue=venue.name,
                climate_day=event.climate_day,
                book=last_book.get(event.market_id),
                market_id=event.market_id,
            )
            for event in ordered
            if event.kind == "book"
        ]
        coverage = build_coverage_report(rows)

    snap = vintage.snapshot().snapshot_id if vintage is not None else snapshot_id_for(())
    return BacktestResult(
        config_hash=config_hash(config),
        prereg_id=str(config.get("prereg_id", registered.get("prereg_id", ""))),
        git_sha=git_sha(),
        snapshot_id=snap,
        seed=int(config.get("seed", 0)),
        universe=tuple(str(item) for item in config.get("universe", ())),
        fills=tuple(fills),
        pnl=pnl,
        coverage=coverage,
        extra={"ledger": [entry.payload for entry in ledger.entries]},
    )
