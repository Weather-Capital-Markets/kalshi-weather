"""Harness: re-export the single MarketView constructor. Strategies never call this."""

from __future__ import annotations

from wxmm.core.types import Order, Side
from wxmm.core.view import book_view_from_record, book_view_from_snapshot, build_market_view
from wxmm.strategy.view import ProposedOrder

__all__ = [
    "book_view_from_record",
    "book_view_from_snapshot",
    "build_market_view",
    "proposed_to_order",
]


def proposed_to_order(proposal: ProposedOrder, *, is_taker: bool = False) -> Order:
    side: Side = "buy" if proposal.side == "buy" else "sell"
    return Order(
        venue=proposal.venue,
        market_id=proposal.market,
        side=side,
        price_cents=proposal.price,
        quantity=proposal.size,
        is_taker=is_taker,
        client_intent_id=proposal.rationale[:80],
    )
