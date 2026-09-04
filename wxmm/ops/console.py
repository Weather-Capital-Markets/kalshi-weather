"""Terminal console: ladders, Σp, fee-adjusted edge, hedge residual, checklist."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from wxmm.core.errors import UnverifiedFactError
from wxmm.core.money import Money
from wxmm.core.types import (
    KALSHI_WEATHER_MAKER_FEE,
    POLYMARKET_FEE_SCHEDULE,
    BookSnapshot,
    VenueFact,
)
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.risk.hedge import HedgePlan
from wxmm.risk.position import Position
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee


@dataclass
class LadderView:
    venue: str
    market_id: str
    book: BookSnapshot | None
    mid_cents: int | None
    executable_buy_cents: int | None
    executable_sell_cents: int | None


@dataclass
class ConsoleState:
    as_of: datetime
    kalshi: tuple[LadderView, ...]
    polymarket: tuple[LadderView, ...]
    positions: tuple[Position, ...]
    hedge: HedgePlan | None
    kalshi_tokens_remaining: float
    polymarket_req_remaining: float
    facts: tuple[VenueFact, ...] = field(
        default_factory=lambda: (KALSHI_WEATHER_MAKER_FEE, POLYMARKET_FEE_SCHEDULE)
    )


def implied_prob_cents(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return Decimal(cents) / Decimal(100)


def sum_p(views: tuple[LadderView, ...], *, executable: bool) -> Decimal | None:
    total = Decimal("0")
    n = 0
    for view in views:
        price = view.executable_buy_cents if executable else view.mid_cents
        if price is None:
            continue
        total += Decimal(price) / Decimal(100)
        n += 1
    if n == 0:
        return None
    return total


def fee_adjusted_edge(
    mid_cents: int,
    fair_cents: int,
    *,
    contracts: int = 1,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
) -> Money:
    """Kalshi-only. Polymarket must not reach here without a verified schedule."""
    price = Decimal(mid_cents) / Decimal(100)
    fee = taker_fee(contracts, price, rounding)
    edge = Money.cents((fair_cents - mid_cents) * contracts)
    return edge - fee


def pretrade_blockers(state: ConsoleState) -> tuple[str, ...]:
    blockers: list[str] = []
    for fact in state.facts:
        try:
            fact.require_for_capital(state.as_of)
        except UnverifiedFactError as exc:
            blockers.append(str(exc))
    if any(view.venue == "polymarket" for view in state.polymarket):
        blockers.append(
            "Polymarket fee schedule UNVERIFIED — cannot price a fill (UnverifiedFeeSchedule)"
        )
    if state.hedge is not None and state.hedge.residual_basis is not None:
        if state.hedge.residual_basis_risk_pct == Decimal("0"):
            blockers.append("cross-venue residual basis reported as zero — forbidden")
    knyc = KALSHI_NYC_DAILY_HIGH
    klga = POLYMARKET_NYC_DAILY_HIGH
    if knyc.fungible(klga):
        blockers.append("Kalshi NYC marked fungible with Polymarket NYC — forbidden")
    return tuple(blockers)


def render(state: ConsoleState) -> str:
    lines: list[str] = [
        f"WXMM console as_of={state.as_of.isoformat()}",
        "NO ORDER ROUTER — propose only; human sends.",
        "",
        "== Kalshi ladders ==",
    ]
    for view in state.kalshi:
        lines.append(_fmt_ladder(view))
    kalshi_mid = sum_p(state.kalshi, executable=False)
    kalshi_exe = sum_p(state.kalshi, executable=True)
    lines.append(f"Kalshi Σp mid={kalshi_mid} executable={kalshi_exe}")
    lines.append("")
    lines.append("== Polymarket ladders ==")
    for view in state.polymarket:
        lines.append(_fmt_ladder(view))
    pm_mid = sum_p(state.polymarket, executable=False)
    pm_exe = sum_p(state.polymarket, executable=True)
    lines.append(f"Polymarket Σp mid={pm_mid} executable={pm_exe} (negRisk mutually exclusive)")
    lines.append("")
    lines.append("== Positions / exposure ==")
    if not state.positions:
        lines.append("(none)")
    for pos in state.positions:
        lines.append(
            f"{pos.venue} {pos.market_id} qty={pos.quantity} "
            f"underlying={pos.underlying.station}/{pos.underlying.revision_rule}"
        )
    lines.append("")
    lines.append("== Hedge plan ==")
    if state.hedge is None:
        lines.append("(none)")
    else:
        lines.append(
            f"efficiency={state.hedge.hedge_efficiency} "
            f"residual_basis_risk_pct={state.hedge.residual_basis_risk_pct} "
            f"rejected={state.hedge.rejected} reason={state.hedge.reason}"
        )
        if state.hedge.residual_basis is not None:
            rb = state.hedge.residual_basis
            lines.append(
                f"residual distribution median_f={rb.median_f} "
                f"disagree={rb.bracket_disagreement_rate} measured={rb.measured} "
                f"(never a point estimate)"
            )
    lines.append("")
    lines.append("== Rate-limit budget ==")
    lines.append(
        f"Kalshi write tokens remaining={state.kalshi_tokens_remaining} "
        f"(~10 orders/s; batch cancel=2 tokens)"
    )
    lines.append(f"Polymarket req/s remaining={state.polymarket_req_remaining} (20/s per IP)")
    lines.append("")
    lines.append("== Pre-trade checklist ==")
    blockers = pretrade_blockers(state)
    if blockers:
        lines.append("BLOCKED:")
        lines.extend(f"  - {item}" for item in blockers)
    else:
        lines.append("clear")
    return "\n".join(lines) + "\n"


def _fmt_ladder(view: LadderView) -> str:
    if view.book is None:
        return f"{view.venue} {view.market_id}: NO BOOK"
    return (
        f"{view.venue} {view.market_id} two_sided={view.book.two_sided} "
        f"mid={view.mid_cents} exe_buy={view.executable_buy_cents} "
        f"exe_sell={view.executable_sell_cents} "
        f"vol={view.book.volume!r} ask_size_known={view.book.ask_size_known} "
        f"stale={view.book.staleness} reconstructed={view.book.reconstructed}"
    )
