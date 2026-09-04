"""Daily venue vs journal reconcile. Any break is loud and blocking.

Allowed to assume
    Positions, fills, and fees are compared. An unreconciled break HALTs.

Must never
    Auto-fix a break. Resume HALT without a journal resolution entry.
    Treat a fee mismatch on a verified schedule as a warning.
"""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.core.errors import FeeMismatch, UnreconciledBreak
from wxmm.core.money import Money
from wxmm.execute.journal import Journal, _as_int


@dataclass(frozen=True, slots=True)
class VenueFill:
    venue_fill_id: str
    market_id: str
    quantity: int
    price_cents: int
    fee: Money | None = None


@dataclass(frozen=True, slots=True)
class VenuePosition:
    venue: str
    market_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str
    intent_id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class BreakReport:
    mismatches: tuple[Mismatch, ...]
    halted: bool

    @property
    def blocking(self) -> bool:
        return len(self.mismatches) > 0


def reconcile(
    journal: Journal,
    venue_fills: tuple[VenueFill, ...] | list[VenueFill],
    *,
    venue_positions: tuple[VenuePosition, ...] | list[VenuePosition] | None = None,
    halt_on_break: bool = True,
) -> BreakReport:
    mismatches: list[Mismatch] = []
    fills = journal.fills_by_intent()
    by_venue_id: dict[str, tuple[str, dict[str, object]]] = {}
    for intent_id, payload in fills.items():
        vid = payload.get("venue_fill_id")
        if isinstance(vid, str):
            by_venue_id[vid] = (intent_id, payload)
    seen: set[str] = set()
    for vfill in venue_fills:
        seen.add(vfill.venue_fill_id)
        hit = by_venue_id.get(vfill.venue_fill_id)
        if hit is None:
            mismatches.append(
                Mismatch("venue_fill_not_in_journal", None, f"venue_fill_id={vfill.venue_fill_id}")
            )
            continue
        intent_id, payload = hit
        if _as_int(payload["fill_qty"]) != vfill.quantity:
            mismatches.append(
                Mismatch(
                    "qty_mismatch",
                    intent_id,
                    f"journal={payload['fill_qty']} venue={vfill.quantity}",
                )
            )
        order = journal.order(intent_id)
        if order.market_id != vfill.market_id:
            mismatches.append(
                Mismatch(
                    "market_mismatch",
                    intent_id,
                    f"journal={order.market_id} venue={vfill.market_id}",
                )
            )
    for intent_id, payload in fills.items():
        vid = payload.get("venue_fill_id")
        if isinstance(vid, str) and vid not in seen:
            mismatches.append(
                Mismatch("journal_fill_not_at_venue", intent_id, f"venue_fill_id={vid}")
            )
    if venue_positions is not None:
        journal_pos = {
            (p.venue, p.market_id): p.quantity for p in journal.positions().all()
        }
        venue_pos = {(p.venue, p.market_id): p.quantity for p in venue_positions}
        for key, qty in venue_pos.items():
            if journal_pos.get(key) != qty:
                mismatches.append(
                    Mismatch(
                        "position_mismatch",
                        None,
                        f"{key}: journal={journal_pos.get(key)!r} venue={qty}",
                    )
                )
        for key, qty in journal_pos.items():
            if key not in venue_pos:
                mismatches.append(
                    Mismatch("journal_position_not_at_venue", None, f"{key} qty={qty}")
                )
    halted = bool(mismatches) and halt_on_break
    return BreakReport(mismatches=tuple(mismatches), halted=halted)


def require_clean(report: BreakReport) -> None:
    if report.blocking:
        raise UnreconciledBreak(
            "; ".join(f"{m.kind}:{m.detail}" for m in report.mismatches)
        )


def check_venue_fee(*, predicted: Money, charged: Money, venue: str) -> None:
    if predicted != charged:
        raise FeeMismatch(f"{venue} predicted={predicted} charged={charged}")
