"""Append-only, content-addressed journal.

Allowed to assume
    Replaying from empty reconstructs byte-identical positions. Duplicate
    venue_fill_id is ignored. Out-of-order fills wait for ACKED.

Must never
    Mutate a prior record. Auto-resolve a reconcile break. Use actor
    ``system``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from wxmm.core.types import Order
from wxmm.core.underlying import Underlying
from wxmm.core.utc import require_utc
from wxmm.execute.lifecycle import Lifecycle, OrderState, Transition
from wxmm.risk.position import Position, PositionBook


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str):
            return int(value)
        raise TypeError(f"expected int, got {type(value).__name__}")
    return value


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass(frozen=True, slots=True)
class JournalRecord:
    seq: int
    content_hash: str
    prev_hash: str
    kind: str
    payload: Mapping[str, object]
    at_utc: datetime
    actor: str
    evidence: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_utc", require_utc(self.at_utc))
        if self.actor not in {"human", "venue"}:
            raise ValueError(f"actor must be human|venue, not {self.actor!r}")


class Journal:
    GENESIS = "genesis"

    def __init__(self) -> None:
        self.records: list[JournalRecord] = []
        self.lifecycle = Lifecycle()
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, dict[str, object]] = {}
        self._seen_fill_ids: set[str] = set()
        self._fill_events: list[dict[str, object]] = []
        self._positions = PositionBook()
        self._underlyings: dict[tuple[str, str], Underlying] = {}
        self._resolutions: list[JournalRecord] = []
        self._settlements: dict[str, dict[str, object]] = {}

    def _append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        at_utc: datetime,
        actor: str,
        evidence: str,
    ) -> JournalRecord:
        prev = self.records[-1].content_hash if self.records else self.GENESIS
        body = {
            "kind": kind,
            "payload": dict(payload),
            "prev": prev,
            "actor": actor,
            "evidence": evidence,
            "at_utc": require_utc(at_utc).isoformat(),
        }
        digest = hashlib.sha256(prev.encode() + _canonical(body)).hexdigest()
        rec = JournalRecord(
            seq=len(self.records),
            content_hash=digest,
            prev_hash=prev,
            kind=kind,
            payload=dict(payload),
            at_utc=at_utc,
            actor=actor,
            evidence=evidence,
        )
        self.records.append(rec)
        return rec

    def propose(
        self,
        intent_id: str,
        order: Order,
        at_utc: datetime,
        *,
        underlying: Underlying,
        evidence: str = "human proposed",
    ) -> JournalRecord:
        self._orders[intent_id] = order
        self._underlyings[(order.venue, order.market_id)] = underlying
        self.lifecycle.advance(
            intent_id,
            OrderState.PROPOSED,
            at_utc=at_utc,
            actor="human",
            evidence=evidence,
        )
        return self._append(
            "proposed",
            {
                "intent_id": intent_id,
                "venue": order.venue,
                "market_id": order.market_id,
                "side": order.side,
                "price_cents": order.price_cents,
                "quantity": order.quantity,
                "is_taker": order.is_taker,
                "underlying_station": underlying.station,
                "underlying_product": underlying.product,
                "underlying_day_convention": underlying.day_convention,
                "underlying_revision_rule": underlying.revision_rule,
            },
            at_utc=at_utc,
            actor="human",
            evidence=evidence,
        )

    def transition(
        self,
        intent_id: str,
        to: OrderState,
        *,
        at_utc: datetime,
        actor: str,
        evidence: str,
        extra: Mapping[str, object] | None = None,
    ) -> tuple[Transition, JournalRecord]:
        tr = self.lifecycle.advance(
            intent_id, to, at_utc=at_utc, actor=actor, evidence=evidence
        )
        payload: dict[str, object] = {
            "intent_id": intent_id,
            "from": str(tr.frm),
            "to": to.value,
        }
        if extra:
            payload.update(dict(extra))
        rec = self._append(
            "transition",
            payload,
            at_utc=at_utc,
            actor=actor,
            evidence=evidence,
        )
        if to is OrderState.ACKED:
            for pending in self.lifecycle.drain_pending_fills(intent_id):
                self.apply_fill(
                    intent_id,
                    fill_qty=_as_int(pending["fill_qty"]),
                    venue_fill_id=str(pending["venue_fill_id"]),
                    at_utc=at_utc,
                    evidence="buffered fill after ack",
                    price_cents=_as_int(pending.get("price_cents") or 0),
                )
        return tr, rec

    def apply_fill(
        self,
        intent_id: str,
        *,
        fill_qty: int,
        venue_fill_id: str,
        at_utc: datetime,
        evidence: str,
        price_cents: int | None = None,
    ) -> JournalRecord | None:
        state = self.lifecycle.state_of(intent_id)
        terminal = {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.SETTLED,
        }
        if state in terminal:
            if venue_fill_id in self._seen_fill_ids:
                return self._append(
                    "fill_duplicate_ignored",
                    {"intent_id": intent_id, "venue_fill_id": venue_fill_id},
                    at_utc=at_utc,
                    actor="venue",
                    evidence="duplicate fill",
                )
            return self._append(
                "fill_after_terminal_ignored",
                {
                    "intent_id": intent_id,
                    "venue_fill_id": venue_fill_id,
                    "fill_qty": fill_qty,
                },
                at_utc=at_utc,
                actor="venue",
                evidence="fill after terminal state ignored",
            )
        if state not in {OrderState.ACKED, OrderState.PARTIAL}:
            self.lifecycle.buffer_fill(
                intent_id,
                {
                    "fill_qty": fill_qty,
                    "venue_fill_id": venue_fill_id,
                    "price_cents": price_cents or 0,
                },
            )
            return self._append(
                "fill_buffered",
                {
                    "intent_id": intent_id,
                    "venue_fill_id": venue_fill_id,
                    "fill_qty": fill_qty,
                    "price_cents": price_cents if price_cents is not None else 0,
                },
                at_utc=at_utc,
                actor="venue",
                evidence=evidence,
            )
        if venue_fill_id in self._seen_fill_ids:
            return self._append(
                "fill_duplicate_ignored",
                {"intent_id": intent_id, "venue_fill_id": venue_fill_id},
                at_utc=at_utc,
                actor="venue",
                evidence="duplicate fill",
            )
        self._seen_fill_ids.add(venue_fill_id)
        order = self._orders[intent_id]
        signed = fill_qty if order.side == "buy" else -fill_qty
        px = price_cents if price_cents is not None else order.price_cents
        underlying = self._underlyings[(order.venue, order.market_id)]
        self._positions.add_fill(
            Position(
                venue=order.venue,
                market_id=order.market_id,
                underlying=underlying,
                quantity=signed,
                avg_price_cents=px,
            )
        )
        prev_qty = _as_int(self._fills.get(intent_id, {}).get("total_qty") or 0)
        total = prev_qty + fill_qty
        event: dict[str, object] = {
            "intent_id": intent_id,
            "venue_fill_id": venue_fill_id,
            "fill_qty": fill_qty,
            "total_qty": total,
            "price_cents": px,
            "market_id": order.market_id,
        }
        self._fill_events.append(event)
        self._fills[intent_id] = event
        rec = self._append(
            "fill",
            event,
            at_utc=at_utc,
            actor="venue",
            evidence=evidence,
        )
        target = OrderState.FILLED if total >= order.quantity else OrderState.PARTIAL
        if self.lifecycle.state_of(intent_id) is not target:
            self.transition(
                intent_id,
                target,
                at_utc=at_utc,
                actor="venue",
                evidence=evidence,
            )
        return rec

    def record_resolution(self, reason: str, *, at_utc: datetime) -> JournalRecord:
        rec = self._append(
            "halt_resolution",
            {"reason": reason},
            at_utc=at_utc,
            actor="human",
            evidence=reason,
        )
        self._resolutions.append(rec)
        return rec

    def record_settlement(
        self,
        intent_id: str,
        *,
        at_utc: datetime,
        high_f: int | None,
        revision: bool,
        evidence: str,
    ) -> JournalRecord:
        prev = self._settlements.get(intent_id)
        self._settlements[intent_id] = {"high_f": high_f, "revision": revision}
        rec = self._append(
            "settlement_revision" if revision else "settlement",
            {
                "intent_id": intent_id,
                "high_f": high_f,
                "revision": revision,
                "previous_high_f": None if prev is None else prev.get("high_f"),
            },
            at_utc=at_utc,
            actor="venue",
            evidence=evidence,
        )
        state = self.lifecycle.state_of(intent_id)
        if state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}:
            self.transition(
                intent_id,
                OrderState.SETTLED,
                at_utc=at_utc,
                actor="venue",
                evidence=evidence,
            )
        elif state is OrderState.SETTLED and revision:
            self._append(
                "settlement_revised_after_settled",
                {"intent_id": intent_id, "high_f": high_f},
                at_utc=at_utc,
                actor="venue",
                evidence=evidence,
            )
        return rec

    def latest_resolution_reason(self) -> str | None:
        if not self._resolutions:
            return None
        reason = self._resolutions[-1].payload.get("reason")
        return str(reason) if reason is not None else None

    def positions(self) -> PositionBook:
        return self._positions

    def fills_by_intent(self) -> dict[str, dict[str, object]]:
        return dict(self._fills)

    def fill_events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._fill_events)

    def order(self, intent_id: str) -> Order:
        return self._orders[intent_id]

    def positions_fingerprint(self) -> tuple[tuple[object, ...], ...]:
        return tuple(
            sorted(
                (
                    p.venue,
                    p.market_id,
                    p.quantity,
                    p.avg_price_cents,
                    p.underlying.station,
                    p.underlying.product,
                    p.underlying.day_convention,
                    p.underlying.revision_rule,
                )
                for p in self._positions.all()
            )
        )


def _payload_int(payload: Mapping[str, object], key: str, default: int = 0) -> int:
    raw = payload.get(key, default)
    if raw is None:
        return default
    return _as_int(raw)


def _order_and_underlying_from_propose(
    payload: Mapping[str, object],
    *,
    seed: Journal | None,
) -> tuple[Order, Underlying]:
    intent = str(payload["intent_id"])
    has_fields = all(
        key in payload
        for key in (
            "venue",
            "market_id",
            "side",
            "price_cents",
            "quantity",
            "underlying_station",
            "underlying_product",
            "underlying_day_convention",
            "underlying_revision_rule",
        )
    )
    if has_fields:
        side = str(payload["side"])
        if side not in {"buy", "sell"}:
            raise ValueError(f"invalid side {side!r}")
        order = Order(
            venue=str(payload["venue"]),
            market_id=str(payload["market_id"]),
            side=side,  # type: ignore[arg-type]
            price_cents=_as_int(payload["price_cents"]),
            quantity=_as_int(payload["quantity"]),
            is_taker=bool(payload.get("is_taker", False)),
            client_intent_id=intent,
        )
        underlying = Underlying(
            station=str(payload["underlying_station"]),
            product=str(payload["underlying_product"]),
            day_convention=str(payload["underlying_day_convention"]),
            revision_rule=str(payload["underlying_revision_rule"]),
        )
        return order, underlying
    if seed is None:
        raise ValueError(
            "propose payload missing order/underlying fields; pass seed= to reconstruct"
        )
    order = seed.order(intent)
    return order, seed._underlyings[(order.venue, order.market_id)]


def replay_journal(records: list[JournalRecord], *, seed: Journal | None = None) -> Journal:
    """Replay from empty records. Reconstructs positions without requiring ``seed``."""
    from wxmm.execute.lifecycle import allowed as transition_allowed

    out = Journal()
    prev = Journal.GENESIS
    for rec in records:
        if rec.prev_hash != prev:
            raise ValueError(f"journal hash break at seq={rec.seq}")
        prev = rec.content_hash
        kind = rec.kind
        p = rec.payload
        if kind == "proposed":
            intent = str(p["intent_id"])
            order, underlying = _order_and_underlying_from_propose(p, seed=seed)
            out.propose(
                intent, order, rec.at_utc, underlying=underlying, evidence=rec.evidence
            )
        elif kind == "transition":
            intent = str(p["intent_id"])
            to = OrderState(str(p["to"]))
            cur = out.lifecycle.state_of(intent)
            if cur is to or not transition_allowed(cur, to):
                continue
            out.transition(
                intent,
                to,
                at_utc=rec.at_utc,
                actor=rec.actor,
                evidence=rec.evidence,
            )
        elif kind == "fill":
            vid = str(p["venue_fill_id"])
            if vid in out._seen_fill_ids:
                continue
            out.apply_fill(
                str(p["intent_id"]),
                fill_qty=_as_int(p["fill_qty"]),
                venue_fill_id=vid,
                at_utc=rec.at_utc,
                evidence=rec.evidence,
                price_cents=_payload_int(p, "price_cents"),
            )
        elif kind == "fill_buffered":
            out.apply_fill(
                str(p["intent_id"]),
                fill_qty=_as_int(p["fill_qty"]),
                venue_fill_id=str(p["venue_fill_id"]),
                at_utc=rec.at_utc,
                evidence=rec.evidence,
                price_cents=_payload_int(p, "price_cents"),
            )
        elif kind in {"fill_duplicate_ignored", "fill_after_terminal_ignored"}:
            out.apply_fill(
                str(p["intent_id"]),
                fill_qty=_payload_int(p, "fill_qty"),
                venue_fill_id=str(p["venue_fill_id"]),
                at_utc=rec.at_utc,
                evidence=rec.evidence,
                price_cents=_payload_int(p, "price_cents"),
            )
        elif kind == "halt_resolution":
            out.record_resolution(str(p["reason"]), at_utc=rec.at_utc)
        elif kind in {"settlement", "settlement_revision"}:
            intent = str(p["intent_id"])
            high = p.get("high_f")
            out.record_settlement(
                intent,
                at_utc=rec.at_utc,
                high_f=high if isinstance(high, int) else None,
                revision=kind == "settlement_revision" or bool(p.get("revision")),
                evidence=rec.evidence,
            )
        elif kind == "settlement_revised_after_settled":
            # Side-effect of record_settlement(revision=True) while already SETTLED.
            continue
    return out
