"""Reconcile hand fills against venue fills. Flag mismatches; do not auto-fix."""

from __future__ import annotations

from dataclasses import dataclass

from wxmm.ops.journal import Journal, JournalState


@dataclass(frozen=True, slots=True)
class VenueFill:
    venue_fill_id: str
    market_id: str
    quantity: int
    price_cents: int


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str
    intent_id: str | None
    detail: str


def reconcile(
    journal: Journal,
    venue_fills: tuple[VenueFill, ...] | list[VenueFill],
) -> tuple[Mismatch, ...]:
    mismatches: list[Mismatch] = []
    filled = journal.by_state(JournalState.FILLED)
    by_venue_id = {item.venue_fill_id: item for item in filled if item.venue_fill_id}
    seen: set[str] = set()
    for vfill in venue_fills:
        seen.add(vfill.venue_fill_id)
        entry = by_venue_id.get(vfill.venue_fill_id)
        if entry is None:
            mismatches.append(
                Mismatch("venue_fill_not_in_journal", None, f"venue_fill_id={vfill.venue_fill_id}")
            )
            continue
        if entry.fill_qty != vfill.quantity:
            mismatches.append(
                Mismatch(
                    "qty_mismatch",
                    entry.intent_id,
                    f"journal={entry.fill_qty} venue={vfill.quantity}",
                )
            )
        if entry.order.market_id != vfill.market_id:
            mismatches.append(
                Mismatch(
                    "market_mismatch",
                    entry.intent_id,
                    f"journal={entry.order.market_id} venue={vfill.market_id}",
                )
            )
    for entry in filled:
        if entry.venue_fill_id and entry.venue_fill_id not in seen:
            mismatches.append(
                Mismatch(
                    "journal_fill_not_at_venue",
                    entry.intent_id,
                    f"venue_fill_id={entry.venue_fill_id}",
                )
            )
    return tuple(mismatches)
