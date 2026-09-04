"""Send gate: token bound to content hash; no send without a token parameter."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wxmm.core.errors import SendTokenError
from wxmm.core.money import Money
from wxmm.core.types import FrozenClock, InMemoryAsOfStore, Order
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH
from wxmm.decide.proposal import Proposal
from wxmm.execute.journal import Journal
from wxmm.execute.lifecycle import OrderState
from wxmm.execute.send_gate import SendGate
from wxmm.venues.fake.adapter import FakeVenue

UTC = timezone.utc
WXMM_EXECUTE = Path(__file__).resolve().parents[3] / "wxmm" / "execute"


def _proposal(**over: object) -> Proposal:
    kwargs: dict[str, object] = {
        "venue": "kalshi",
        "market": "M",
        "side": "buy",
        "price": 40,
        "size": 2,
        "modelled_fee": Money.cents(0),
        "modelled_collateral": Money.cents(80),
        "edge": None,
        "limit_checks_passed": ("size_known",),
        "rate_limit_cost": 10.0,
        "rationale": "test quote",
    }
    kwargs.update(over)
    return Proposal(**kwargs)  # type: ignore[arg-type]


def _gate(clock: FrozenClock | None = None) -> tuple[SendGate, FakeVenue, FrozenClock]:
    clk = clock or FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    venue = FakeVenue(InMemoryAsOfStore())
    journal = Journal()
    return SendGate(venue, journal, clk), venue, clk


def _proposed(gate: SendGate, proposal: Proposal, ts: datetime) -> None:
    order = Order(
        venue=proposal.venue,
        market_id=proposal.market,
        side=proposal.side,  # type: ignore[arg-type]
        price_cents=proposal.price,
        quantity=proposal.size,
        is_taker=False,
        client_intent_id="i1",
    )
    gate.journal.propose("i1", order, ts, underlying=KALSHI_NYC_DAILY_HIGH)


def test_token_bound_to_hash_mutation_invalidates() -> None:
    gate, _venue, clock = _gate()
    proposal = _proposal()
    _proposed(gate, proposal, clock.now())
    token = gate.issue_token(proposal)
    mutated = _proposal(price=41)
    with pytest.raises(SendTokenError, match="not bound"):
        gate.send(mutated, token, intent_id="i1")
    mutated_size = _proposal(size=3)
    token2 = gate.issue_token(proposal)
    with pytest.raises(SendTokenError, match="not bound"):
        gate.send(mutated_size, token2, intent_id="i1")
    mutated_mkt = _proposal(market="OTHER")
    token3 = gate.issue_token(proposal)
    with pytest.raises(SendTokenError, match="not bound"):
        gate.send(mutated_mkt, token3, intent_id="i1")


def test_token_expiry_and_reuse() -> None:
    clock = FrozenClock(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    gate, venue, _ = _gate(clock)
    proposal = _proposal()
    _proposed(gate, proposal, clock.now())
    token = gate.issue_token(proposal)
    clock.set(clock.now() + timedelta(seconds=61))
    with pytest.raises(SendTokenError, match="expired"):
        gate.send(proposal, token, intent_id="i1")
    clock.set(datetime(2026, 7, 4, 16, 0, tzinfo=UTC))
    gate2, venue2, clock2 = _gate()
    _proposed(gate2, proposal, clock2.now())
    tok = gate2.issue_token(proposal)
    result = gate2.send(proposal, tok, intent_id="i1")
    assert result.accepted is True
    assert venue2.submitted
    assert gate2.journal.lifecycle.state_of("i1") is OrderState.ACKED
    with pytest.raises(SendTokenError, match="already used"):
        gate2.send(proposal, tok, intent_id="i1")


def test_send_requires_token_no_default() -> None:
    sig = inspect.signature(SendGate.send)
    token = sig.parameters["token"]
    assert token.default is inspect.Parameter.empty
    assert "force" not in sig.parameters


def test_public_execute_send_paths_require_token() -> None:
    import wxmm.execute as pkg

    offenders: list[str] = []
    for name in pkg.__all__:
        obj = getattr(pkg, name)
        if inspect.isclass(obj) and obj.__module__.startswith("wxmm.execute"):
            for meth_name, meth in inspect.getmembers(obj, predicate=inspect.isfunction):
                if meth_name.startswith("_"):
                    continue
                if "send" in meth_name.lower() or meth_name == "submit":
                    params = inspect.signature(meth).parameters
                    if "token" not in params:
                        offenders.append(f"{obj.__name__}.{meth_name}")
                    elif params["token"].default is not inspect.Parameter.empty:
                        offenders.append(f"{obj.__name__}.{meth_name} default token")
        if inspect.isfunction(obj) and (
            "send" in name.lower() or name == "submit"
        ):
            params = inspect.signature(obj).parameters
            if "token" not in params or params["token"].default is not inspect.Parameter.empty:
                offenders.append(name)
    assert offenders == []


def test_no_force_flag_and_no_token_default_in_execute_source() -> None:
    hits: list[str] = []
    for path in WXMM_EXECUTE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "send":
                for arg, default in zip(
                    node.args.args[-len(node.args.defaults) :] if node.args.defaults else [],
                    node.args.defaults,
                ):
                    if arg.arg == "token":
                        hits.append(f"{path.name}:{node.lineno} token default")
                    if arg.arg == "force":
                        hits.append(f"{path.name}:{node.lineno} force")
            if isinstance(node, ast.FunctionDef):
                names = [a.arg for a in node.args.args]
                if "force" in names:
                    hits.append(f"{path.name}:{node.lineno} force param")
    assert hits == []
