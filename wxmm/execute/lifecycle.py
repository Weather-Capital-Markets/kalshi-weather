"""Explicit one-way order lifecycle.

Allowed to assume
    Transitions are total over the declared graph. Actor is ``human`` or
    ``venue`` — never ``system``.

Must never
    Skip ACKED on the way to FILLED. Skip a terminal fill state on the way
    to SETTLED. Invent a reverse transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from wxmm.core.errors import InvalidTransition
from wxmm.core.utc import require_utc


class OrderState(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    SETTLED = "SETTLED"


TERMINAL_FILL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED})

GRAPH: dict[OrderState | None, frozenset[OrderState]] = {
    None: frozenset({OrderState.PROPOSED}),
    OrderState.PROPOSED: frozenset({OrderState.APPROVED}),
    OrderState.APPROVED: frozenset({OrderState.SENT}),
    OrderState.SENT: frozenset({OrderState.ACKED, OrderState.REJECTED}),
    OrderState.ACKED: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
    ),
    OrderState.FILLED: frozenset({OrderState.SETTLED}),
    OrderState.CANCELLED: frozenset({OrderState.SETTLED}),
    OrderState.REJECTED: frozenset({OrderState.SETTLED}),
    OrderState.SETTLED: frozenset(),
}

HUMAN_STATES = frozenset({OrderState.PROPOSED, OrderState.APPROVED, OrderState.SENT})


@dataclass(frozen=True, slots=True)
class Transition:
    intent_id: str
    frm: OrderState | None
    to: OrderState
    at_utc: datetime
    actor: str
    evidence: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_utc", require_utc(self.at_utc))
        if self.actor not in {"human", "venue"}:
            raise ValueError(f"actor must be human|venue, not {self.actor!r}")


def allowed(frm: OrderState | None, to: OrderState) -> bool:
    return to in GRAPH.get(frm, frozenset())


def require_transition(frm: OrderState | None, to: OrderState) -> None:
    if not allowed(frm, to):
        raise InvalidTransition(f"{frm} -> {to} is not in the lifecycle graph")
    if to is OrderState.FILLED and frm not in {OrderState.ACKED, OrderState.PARTIAL}:
        raise InvalidTransition("FILLED requires ACKED or PARTIAL")
    if to is OrderState.SETTLED and frm not in TERMINAL_FILL:
        raise InvalidTransition("SETTLED requires FILLED|CANCELLED|REJECTED")


class Lifecycle:
    def __init__(self) -> None:
        self._state: dict[str, OrderState] = {}
        self.history: list[Transition] = []
        self._pending_fills: dict[str, list[dict[str, object]]] = {}

    def state_of(self, intent_id: str) -> OrderState | None:
        return self._state.get(intent_id)

    def advance(
        self,
        intent_id: str,
        to: OrderState,
        *,
        at_utc: datetime,
        actor: str,
        evidence: str,
    ) -> Transition:
        frm = self._state.get(intent_id)
        require_transition(frm, to)
        if to in HUMAN_STATES and actor != "human":
            raise InvalidTransition(f"{to} requires actor=human")
        if to in {OrderState.ACKED, OrderState.PARTIAL, OrderState.FILLED} and actor != "venue":
            if to is not OrderState.CANCELLED:
                raise InvalidTransition(f"{to} requires actor=venue")
        tr = Transition(
            intent_id=intent_id,
            frm=frm,
            to=to,
            at_utc=at_utc,
            actor=actor,
            evidence=evidence,
        )
        self._state[intent_id] = to
        self.history.append(tr)
        return tr

    def buffer_fill(self, intent_id: str, payload: dict[str, object]) -> None:
        """Out-of-order fill before ACK is buffered, never applied as FILLED."""
        self._pending_fills.setdefault(intent_id, []).append(payload)

    def drain_pending_fills(self, intent_id: str) -> list[dict[str, object]]:
        return self._pending_fills.pop(intent_id, [])
