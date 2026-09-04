"""Human confirmation token. Sending without a token is inexpressible.

Allowed to assume
    The token is bound to the proposal content hash (venue, market, side,
    price, size). TTL is 60 seconds. Single use.

Must never
    Default the token parameter. Provide ``force``. Bulk-approve.
    Auto-renew. Call ``datetime.now``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from wxmm.core.errors import InvalidTransition, RateLimited, SendTokenError
from wxmm.core.types import Clock, Order
from wxmm.core.utc import require_utc
from wxmm.decide.proposal import Proposal
from wxmm.execute.journal import Journal
from wxmm.execute.lifecycle import OrderState


class VenueAck(Protocol):
    accepted: bool
    rejected: bool
    venue_order_id: str
    reason: str


class OrderSink(Protocol):
    def submit(self, order: Order) -> VenueAck:
        ...


@dataclass(frozen=True, slots=True)
class ConfirmationToken:
    token_id: str
    proposal_hash: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", require_utc(self.issued_at))
        object.__setattr__(self, "expires_at", require_utc(self.expires_at))


@dataclass(frozen=True, slots=True)
class SendResult:
    intent_id: str
    accepted: bool
    venue_order_id: str | None
    state: OrderState
    detail: str


class SendGate:
    def __init__(self, sink: OrderSink, journal: Journal, clock: Clock) -> None:
        self._sink = sink
        self.journal = journal
        self.clock = clock
        self._issued: dict[str, ConfirmationToken] = {}
        self._used: set[str] = set()

    def issue_token(self, proposal: Proposal) -> ConfirmationToken:
        now = self.clock.now()
        token = ConfirmationToken(
            token_id=secrets.token_hex(16),
            proposal_hash=proposal.content_hash(),
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        self._issued[token.token_id] = token
        return token

    def send(
        self,
        proposal: Proposal,
        token: ConfirmationToken,
        *,
        intent_id: str,
    ) -> SendResult:
        now = self.clock.now()
        self._validate(proposal, token, now)
        self._used.add(token.token_id)
        state = self.journal.lifecycle.state_of(intent_id)
        if state is OrderState.PROPOSED:
            self.journal.transition(
                intent_id,
                OrderState.APPROVED,
                at_utc=now,
                actor="human",
                evidence=f"token:{token.token_id}",
            )
        elif state is not OrderState.APPROVED:
            raise InvalidTransition(f"send requires PROPOSED or APPROVED, not {state}")
        order = Order(
            venue=proposal.venue,
            market_id=proposal.market,
            side=proposal.side,  # type: ignore[arg-type]
            price_cents=proposal.price,
            quantity=proposal.size,
            is_taker=False,
            client_intent_id=intent_id,
        )
        try:
            ack = self._sink.submit(order)
        except RateLimited as exc:
            return SendResult(
                intent_id=intent_id,
                accepted=False,
                venue_order_id=None,
                state=OrderState.APPROVED,
                detail=f"429 retry_after={exc.retry_after!r}",
            )
        except ConnectionError as exc:
            return SendResult(
                intent_id=intent_id,
                accepted=False,
                venue_order_id=None,
                state=OrderState.APPROVED,
                detail=f"disconnect:{exc}",
            )
        self.journal.transition(
            intent_id,
            OrderState.SENT,
            at_utc=now,
            actor="human",
            evidence=f"token:{token.token_id}",
        )
        if ack.rejected or not ack.accepted:
            self.journal.transition(
                intent_id,
                OrderState.REJECTED,
                at_utc=now,
                actor="venue",
                evidence=ack.reason or "rejected",
            )
            return SendResult(
                intent_id=intent_id,
                accepted=False,
                venue_order_id=ack.venue_order_id,
                state=OrderState.REJECTED,
                detail=ack.reason,
            )
        self.journal.transition(
            intent_id,
            OrderState.ACKED,
            at_utc=now,
            actor="venue",
            evidence=ack.venue_order_id,
        )
        return SendResult(
            intent_id=intent_id,
            accepted=True,
            venue_order_id=ack.venue_order_id,
            state=OrderState.ACKED,
            detail="acked",
        )

    def _validate(self, proposal: Proposal, token: ConfirmationToken, now: datetime) -> None:
        stored = self._issued.get(token.token_id)
        if stored is None:
            raise SendTokenError("unknown confirmation token")
        if token.token_id in self._used:
            raise SendTokenError("confirmation token already used")
        if stored.proposal_hash != proposal.content_hash():
            raise SendTokenError("token is not bound to this proposal content hash")
        if now >= stored.expires_at:
            raise SendTokenError("confirmation token expired")
