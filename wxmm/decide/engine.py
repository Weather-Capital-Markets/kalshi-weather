"""Ranked proposals from a MarketView. Operable with no fair-value model.

Allowed to assume
    ``system_status`` is ``OK``, ``DEGRADED``, or ``HALT`` (string, so this
    module does not import ``wxmm.monitor`` or ``wxmm.execute``).
    Fair value may be None.

Must never
    Import ``wxmm.execute`` or ``wxmm.live``. Emit a proposal under HALT.
    Assume FV exists. Silently clip a size. Send anything.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from wxmm.core.errors import LimitBreach, UnverifiedFeeSchedule
from wxmm.core.money import Money
from wxmm.core.ratelimit import TokenBucket, proposal_cost
from wxmm.core.types import Order
from wxmm.decide.fairvalue import FairValueProvider, NullFairValue
from wxmm.decide.proposal import Proposal
from wxmm.risk.limits import Limits
from wxmm.strategy.view import BookView, MarketView
from wxmm.venues.base import modelled_fee


def _rank_key(p: Proposal) -> tuple[object, ...]:
    return (
        p.rate_blocked,
        0 if p.edge is not None else 1,
        -(p.edge or Decimal("0")),
        p.venue,
        p.market,
        p.side,
    )


def propose(
    view: MarketView,
    *,
    fair: FairValueProvider | None = None,
    system_status: str = "OK",
    degradation: tuple[str, ...] = (),
    buckets: dict[str, TokenBucket] | None = None,
    limits: Limits | None = None,
) -> list[Proposal]:
    """Emit ranked quote candidates. Empty under HALT."""
    if system_status == "HALT":
        return []
    provider = fair if fair is not None else NullFairValue()
    fv = provider.fair(view)
    degr = degradation if system_status == "DEGRADED" else ()
    if system_status == "DEGRADED" and not degr:
        degr = ("degraded",)
    out: list[Proposal] = []
    for book in view.books:
        out.extend(
            _from_book(
                book,
                fv=fv,
                degradation=degr,
                limits=limits,
            )
        )
    out.sort(key=_rank_key)
    if buckets:
        remaining = {venue: bucket.remaining() for venue, bucket in buckets.items()}
        allocated: list[Proposal] = []
        for proposal in out:
            left = remaining.get(proposal.venue)
            blocked = proposal.rate_blocked
            if left is not None:
                if left + 1e-12 < proposal.rate_limit_cost:
                    blocked = True
                elif not blocked:
                    remaining[proposal.venue] = left - proposal.rate_limit_cost
            if blocked and not proposal.rate_blocked:
                rationale = proposal.rationale
                if "; RATE_BLOCKED" not in rationale:
                    rationale = rationale + "; RATE_BLOCKED"
                proposal = replace(proposal, rate_blocked=True, rationale=rationale)
            allocated.append(proposal)
        allocated.sort(key=_rank_key)
        return allocated
    return out


def _from_book(
    book: BookView,
    *,
    fv: Mapping[str, str] | None,
    degradation: tuple[str, ...],
    limits: Limits | None,
) -> list[Proposal]:
    if not book.two_sided or book.bid_cents is None or book.ask_cents is None:
        return []
    candidates: list[tuple[str, int, int | None]] = [
        ("buy", book.bid_cents, book.bid_size),
        ("sell", book.ask_cents, book.ask_size),
    ]
    fair_px: Decimal | None = None
    if fv is not None and book.market_id in fv:
        fair_px = Decimal(fv[book.market_id])
    proposals: list[Proposal] = []
    for side, price, size in candidates:
        if size is None or size <= 0:
            continue
        qty = 1
        edge: Decimal | None = None
        if fair_px is not None:
            p = Decimal(price) / Decimal(100)
            edge = (fair_px - p) if side == "buy" else (p - fair_px)
        rationale = (
            f"maker {side} at touch {price}c on {book.venue}:{book.market_id}; " "no fair value"
            if fair_px is None
            else f"maker {side} at touch {price}c; modelled edge {edge}"
        )
        order = Order(
            venue=book.venue,
            market_id=book.market_id,
            side=side,  # type: ignore[arg-type]
            price_cents=price,
            quantity=qty,
            is_taker=False,
        )
        fee: Money | None
        fee_unverified = False
        try:
            fee = modelled_fee(book.venue, order)
        except UnverifiedFeeSchedule:
            fee = None
            fee_unverified = True
        collateral = Money.cents(price * qty)
        passed: list[str] = ["size_known"]
        if limits is not None:
            try:
                limits.check_collateral(collateral)
                passed.append("max_collateral_committed")
                limits.check_notional_per_climate_day(collateral)
                passed.append("max_notional_per_climate_day")
            except LimitBreach:
                continue
        cost = proposal_cost(book.venue)
        proposals.append(
            Proposal(
                venue=book.venue,
                market=book.market_id,
                side=side,
                price=price,
                size=qty,
                modelled_fee=fee,
                modelled_collateral=collateral,
                edge=edge,
                limit_checks_passed=tuple(passed),
                rate_limit_cost=cost,
                rationale=rationale,
                rate_blocked=False,
                degradation=degradation,
                fee_unverified=fee_unverified,
            )
        )
    return proposals
