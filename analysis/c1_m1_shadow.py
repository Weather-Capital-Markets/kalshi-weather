"""No-capital shadow of TRADE_DERIVED fair value against a recorded tape.

Does not import wxmm.live or wxmm.execute. Does not send. The human never
hands this a send gate. A null (β = 0) model still generates fill and
mark-out observations for K4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from wxmm.core.types import BookSnapshot, Order, Trade
from wxmm.core.view import book_view_from_snapshot
from wxmm.decide.engine import propose
from wxmm.decide.proposal import Proposal
from wxmm.fairvalue.provider import TradeDerivedFairValue
from wxmm.measure.adverse import FillMark, marks_for_fill
from wxmm.measure.shadow import ShadowObservation, ShadowReport, compare_quote, report
from wxmm.strategy.view import MarketView


@dataclass(frozen=True, slots=True)
class RestingQuote:
    proposal: Proposal
    order: Order


@dataclass(frozen=True, slots=True)
class ShadowRun:
    quotes: tuple[RestingQuote, ...]
    shadow: ShadowReport
    markouts: tuple[FillMark, ...]
    is_strategy_pnl: bool = False


def quotes_the_model_would_rest(
    view: MarketView,
    provider: TradeDerivedFairValue,
    *,
    venue: str = "kalshi",
) -> tuple[RestingQuote, ...]:
    """Call propose with the provider. Never send."""
    _ = venue
    proposals = propose(view, fair=provider, system_status="OK")
    out: list[RestingQuote] = []
    for proposal in proposals:
        if proposal.rate_blocked:
            continue
        order = Order(
            venue=proposal.venue,
            market_id=proposal.market,
            side=proposal.side,  # type: ignore[arg-type]
            price_cents=proposal.price,
            quantity=proposal.size,
            is_taker=False,
        )
        out.append(RestingQuote(proposal=proposal, order=order))
    return tuple(out)


def view_from_snapshots(
    snapshots: Sequence[BookSnapshot],
    *,
    venue: str = "kalshi",
) -> MarketView:
    books = tuple(book_view_from_snapshot(venue, snap) for snap in snapshots)
    return MarketView(books=books, positions=(), fills=())


def run_shadow(
    *,
    snapshots: Sequence[BookSnapshot],
    provider: TradeDerivedFairValue,
    subsequent_trades: Mapping[str, Sequence[Trade]],
    real_filled_qty: Mapping[str, int],
    mids: Mapping[str, dict[datetime, int]] | None = None,
    settlement_mid_cents: Mapping[str, int] | None = None,
    venue: str = "kalshi",
) -> ShadowRun:
    """Paper last-in-queue vs real fills + mark-outs. No capital."""
    view = view_from_snapshots(snapshots, venue=venue)
    quotes = quotes_the_model_would_rest(view, provider, venue=venue)
    by_market = {snap.market_id: snap for snap in snapshots}
    observations: list[ShadowObservation] = []
    markouts: list[FillMark] = []
    for quote in quotes:
        book = by_market.get(quote.order.market_id)
        if book is None:
            continue
        trades = tuple(subsequent_trades.get(quote.order.market_id, ()))
        real_qty = int(real_filled_qty.get(quote.order.market_id, 0))
        obs = compare_quote(quote.order, book, trades, real_qty)
        observations.append(obs)
        if real_qty > 0 and mids is not None:
            market_mids = mids.get(quote.order.market_id, {})
            settle = None
            if settlement_mid_cents is not None:
                settle = settlement_mid_cents.get(quote.order.market_id)
            markouts.extend(
                marks_for_fill(
                    fill_id=f"{quote.order.market_id}:{quote.order.side}",
                    fill_ts=book.valid_at,
                    fill_price_cents=quote.order.price_cents,
                    side=quote.order.side,
                    mids=market_mids,
                    settlement_mid_cents=settle,
                )
            )
    return ShadowRun(
        quotes=quotes,
        shadow=report(observations),
        markouts=tuple(markouts),
        is_strategy_pnl=False,
    )
