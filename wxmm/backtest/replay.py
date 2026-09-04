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
from wxmm.backtest.harness import build_market_view, proposed_to_order
from wxmm.backtest.ledger import (
    BacktestResult,
    HeldOutUnlock,
    Ledger,
    config_hash,
    git_sha,
    refuse_if_held_out_locked,
    refuse_unless_preregistered,
)
from wxmm.backtest.settle import (
    SettlementFeePolicy,
    binary_payout_cents,
    polymarket_resolution_path,
    resolve_at,
)
from wxmm.core.money import Money
from wxmm.core.types import (
    AsOfRecord,
    BookSnapshot,
    ClockBoundStore,
    InMemoryAsOfStore,
    Order,
    Trade,
)
from wxmm.core.utc import require_utc
from wxmm.data.quality import CoverageRow, build_coverage_report, row_from_book
from wxmm.data.vintage import VintageStore, snapshot_id_for
from wxmm.settlement.rules import Observation
from wxmm.strategy.protocol import Strategy
from wxmm.strategy.view import FillView, PositionView
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
    observations: Sequence[Observation] | None = None,
    settlement_fee_policy: SettlementFeePolicy | None = None,
    yes_if_at_least: Mapping[str, int] | None = None,
) -> BacktestResult:
    """Run a backtest. Refuses config hashes not in ``prereg/``.

    ``coverage_rows`` must be supplied (or derived from ``books_by_event``);
    a result without a coverage report is invalid.

    Strategies receive a harness-built ``MarketView`` only — never the store,
    clock, venue adapter, or settlement handle.
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
    extra: dict[str, Any] = {"ledger": [entry.payload for entry in ledger.entries]}
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
            extra=extra,
        )

    clock = SimClock(ordered[0].ts)
    store = ClockBoundStore(inner_store or InMemoryAsOfStore(), clock)
    fills: list[Fill] = []
    fill_views: list[FillView] = []
    pnl = Money.zero()
    last_book: dict[str, BookSnapshot] = {}
    lots: dict[str, tuple[int, int]] = {}
    climate_for_market: dict[str, str] = {}
    trades_by_market = trades_by_market or {}

    for index, event in enumerate(ordered):
        clock.advance_to(event.ts)
        climate_for_market[event.market_id] = event.climate_day
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
        positions = tuple(
            PositionView(venue=venue.name, market_id=mid, quantity=qty, avg_price_cents=px)
            for mid, (qty, px) in lots.items()
        )
        book_keys = tuple((venue.name, f"book:{mid}") for mid in last_book)
        view = build_market_view(
            store=store,
            clock=clock,
            book_keys=book_keys,
            positions=positions,
            fills=tuple(fill_views),
        )
        with wall_clock_guard():
            proposed = list(strategy.on_snapshot(view))
        for proposal in proposed:
            order = proposed_to_order(proposal)
            book = last_book.get(order.market_id)
            if books_by_event is not None and index in books_by_event:
                maybe = books_by_event[index]
                if maybe is not None and maybe.market_id == order.market_id:
                    book = maybe
            if book is None:
                continue
            fill = last_in_queue_fill(order, book, trades_by_market.get(order.market_id, ()))
            fills.append(fill)
            if fill.status in {"filled", "partial"} and fill.filled_qty:
                pnl = pnl - fill_cost(venue, order, rounding)
                signed = fill.filled_qty if order.side == "buy" else -fill.filled_qty
                prev_qty, _prev_px = lots.get(order.market_id, (0, fill.price_cents))
                lots[order.market_id] = (prev_qty + signed, fill.price_cents)
                fill_views.append(
                    FillView(
                        venue=order.venue,
                        market_id=order.market_id,
                        side=order.side,
                        price_cents=fill.price_cents,
                        quantity=fill.filled_qty,
                    )
                )

    if observations is not None:
        extra["polymarket_revision_flips"] = _polymarket_flips(
            venue.name, climate_days, observations, as_of=clock.now()
        )
        extra["settlements"] = _settle_open_lots(
            venue_name=venue.name,
            lots=lots,
            climate_for_market=climate_for_market,
            observations=observations,
            as_of=clock.now(),
            policy=settlement_fee_policy or SettlementFeePolicy(),
            yes_if_at_least=yes_if_at_least or {},
        )
        for row in extra["settlements"]:
            pnl = pnl + Money(row["pnl"])

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
    extra["ledger"] = [entry.payload for entry in ledger.entries]
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
        extra=extra,
    )


def _polymarket_flips(
    venue_name: str,
    climate_days: tuple[date, ...],
    observations: Sequence[Observation],
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    if venue_name != "polymarket":
        return []
    flips: list[dict[str, Any]] = []
    seen: set[date] = set()
    for day in climate_days:
        if day in seen:
            continue
        seen.add(day)
        path = polymarket_resolution_path(day, observations, as_of_final=as_of)
        if path.flipped:
            flips.append(
                {
                    "climate_day": day.isoformat(),
                    "initial_high_f": path.initial_high_f,
                    "final_high_f": path.final_high_f,
                    "initial_as_of": path.initial_as_of.isoformat(),
                    "final_as_of": path.final_as_of.isoformat(),
                }
            )
    return flips


def _settle_open_lots(
    *,
    venue_name: str,
    lots: Mapping[str, tuple[int, int]],
    climate_for_market: Mapping[str, str],
    observations: Sequence[Observation],
    as_of: datetime,
    policy: SettlementFeePolicy,
    yes_if_at_least: Mapping[str, int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market_id, (qty, entry_cents) in lots.items():
        if qty == 0:
            continue
        climate = date.fromisoformat(climate_for_market[market_id])
        result = resolve_at(venue_name, climate, observations, as_of)
        dummy = Order(
            venue=venue_name,
            market_id=market_id,
            side="buy" if qty > 0 else "sell",
            price_cents=entry_cents,
            quantity=abs(qty),
            is_taker=False,
        )
        fee = policy.charge(dummy)
        threshold = yes_if_at_least.get(market_id)
        settle_cents = (
            binary_payout_cents(result.high_f, yes_if_at_least=threshold)
            if threshold is not None
            else None
        )
        if settle_cents is None:
            rows.append(
                {
                    "market_id": market_id,
                    "climate_day": climate.isoformat(),
                    "high_f": result.high_f,
                    "pending": True,
                    "pnl": "0",
                }
            )
            continue
        settled_pnl = Money.cents((settle_cents - entry_cents) * qty) - fee
        rows.append(
            {
                "market_id": market_id,
                "climate_day": climate.isoformat(),
                "high_f": result.high_f,
                "pending": result.pending,
                "pnl": str(settled_pnl.amount),
            }
        )
    return rows
