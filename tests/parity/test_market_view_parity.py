"""Live vs replay MarketView parity.

The live path and the replay path must produce structurally identical
``MarketView`` objects from the same ``BookUpdate`` tape. Both call
``wxmm.core.view.build_market_view``.
"""

from __future__ import annotations

import ast
from dataclasses import astuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wxmm.core.errors import StaleBookError
from wxmm.core.types import ClockBoundStore, FrozenClock, InMemoryAsOfStore
from wxmm.core.view import build_market_view
from wxmm.live.state import (
    BookUpdate,
    LiveState,
    apply_book_update,
    book_store_key,
    snapshot_payload,
    snapshot_update,
    stale_update,
)
from wxmm.strategy.view import BookView, MarketView

UTC = timezone.utc
WXMM = Path(__file__).resolve().parents[2] / "wxmm"
KEY = book_store_key("kalshi", "KXHIGHNY-26JUL04-T90")
BOOK_KEYS = (("kalshi", KEY),)
T0 = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)


def _payload(bid: int, ask: int, bid_size: int = 5, ask_size: int = 5) -> dict[str, object]:
    return snapshot_payload(
        market_id="KXHIGHNY-26JUL04-T90",
        bid_cents=bid,
        ask_cents=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        volume=1,
        two_sided=True,
    )


def _snap(bid: int, ask: int, *, at: datetime, received: datetime) -> BookUpdate:
    return snapshot_update(
        venue="kalshi",
        market_id="KXHIGHNY-26JUL04-T90",
        valid_at=at,
        available_at=received,
        payload=_payload(bid, ask),
        source="parity",
    )


def _views_at(
    updates: list[BookUpdate],
    clock_ts: datetime,
) -> tuple[MarketView, MarketView]:
    clock = FrozenClock(clock_ts)
    live = LiveState(clock)
    inner = InMemoryAsOfStore()
    replay = ClockBoundStore(inner, clock)
    for update in updates:
        if update.available_at <= clock_ts:
            live.apply(update)
            apply_book_update(replay, update)
    live_view = build_market_view(store=live, clock=clock, book_keys=BOOK_KEYS)
    replay_view = build_market_view(store=replay, clock=clock, book_keys=BOOK_KEYS)
    return live_view, replay_view


def test_recorded_session_live_and_replay_views_are_structurally_identical() -> None:
    tape = [
        _snap(40, 42, at=T0, received=T0),
        _snap(41, 43, at=T0 + timedelta(seconds=5), received=T0 + timedelta(seconds=5)),
        _snap(39, 41, at=T0 + timedelta(seconds=10), received=T0 + timedelta(seconds=10)),
    ]
    ticks = [T0, T0 + timedelta(seconds=5), T0 + timedelta(seconds=10)]
    live_seq: list[tuple[object, ...]] = []
    replay_seq: list[tuple[object, ...]] = []
    for tick in ticks:
        applied = [u for u in tape if u.available_at <= tick]
        live_view, replay_view = _views_at(applied, tick)
        live_seq.append(astuple(live_view))
        replay_seq.append(astuple(replay_view))
        assert live_view == replay_view
        assert astuple(live_view) == astuple(replay_view)
    assert live_seq == replay_seq
    assert live_seq[0][0][0][3] == 40  # bid_cents of first book
    assert live_seq[-1][0][0][3] == 39


def test_stale_raises_on_both_paths() -> None:
    snap = _snap(40, 42, at=T0, received=T0)
    stale = stale_update(
        venue="kalshi",
        market_id="KXHIGHNY-26JUL04-T90",
        valid_at=T0 + timedelta(seconds=1),
        available_at=T0 + timedelta(seconds=1),
        source="reconnect",
    )
    clock = FrozenClock(T0 + timedelta(seconds=1))
    live = LiveState(clock)
    live.apply(snap)
    live.apply(stale)
    inner = InMemoryAsOfStore()
    replay = ClockBoundStore(inner, clock)
    apply_book_update(replay, snap)
    apply_book_update(replay, stale)
    with pytest.raises(StaleBookError):
        build_market_view(store=live, clock=clock, book_keys=BOOK_KEYS)
    with pytest.raises(StaleBookError):
        build_market_view(store=replay, clock=clock, book_keys=BOOK_KEYS)


def test_reconnect_marks_all_stale_until_snapshot() -> None:
    clock = FrozenClock(T0)
    live = LiveState(clock)
    live.apply(_snap(40, 42, at=T0, received=T0))
    live.mark_all_stale(source="reconnect")
    with pytest.raises(StaleBookError):
        live.get(KEY, as_of=T0)
    clock.set(T0 + timedelta(seconds=2))
    live.apply(_snap(44, 46, at=T0 + timedelta(seconds=2), received=T0 + timedelta(seconds=2)))
    rec = live.get(KEY, as_of=clock.now())
    assert rec.availability == "known"


def test_parity_is_structural_not_summary() -> None:
    a, _ = _views_at([_snap(40, 42, at=T0, received=T0, )], T0)
    # Same mid, different sizes — summaries that only keep mid would collide.
    other_clock = FrozenClock(T0)
    live = LiveState(other_clock)
    live.apply(
        snapshot_update(
            venue="kalshi",
            market_id="KXHIGHNY-26JUL04-T90",
            valid_at=T0,
            available_at=T0,
            payload=_payload(40, 42, bid_size=99, ask_size=1),
            source="parity",
        )
    )
    b = build_market_view(store=live, clock=other_clock, book_keys=BOOK_KEYS)
    assert a.books[0].bid_cents == b.books[0].bid_cents
    assert astuple(a) != astuple(b)


def test_live_package_does_not_construct_market_view() -> None:
    hits: list[str] = []
    live_root = WXMM / "live"
    for path in live_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "MarketView":
                hits.append(f"{path.relative_to(WXMM.parent)}:{node.lineno}")
    assert hits == []


def test_market_view_call_in_wxmm_only_in_core_view() -> None:
    allowed = {WXMM / "core" / "view.py"}
    hits: list[str] = []
    for path in WXMM.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "MarketView":
                hits.append(f"{path.relative_to(WXMM.parent)}:{node.lineno}")
    assert hits == [], "MarketView() must only be constructed in wxmm/core/view.py:\n" + "\n".join(
        hits
    )


def _forked_live_builder(
    *,
    store: object,
    clock: FrozenClock,
    book_keys: tuple[tuple[str, str], ...],
) -> MarketView:
    """Deliberate live-only fork. Parity must reject this."""
    view = build_market_view(store=store, clock=clock, book_keys=book_keys)
    extra = BookView(
        venue="ghost",
        market_id="added-only-on-live",
        two_sided=False,
        bid_cents=1,
        ask_cents=2,
        bid_size=1,
        ask_size=1,
        ask_size_known=True,
        volume=None,
        reconstructed=False,
        staleness=timedelta(0),
    )
    return MarketView(
        books=view.books + (extra,),
        positions=view.positions,
        fills=view.fills,
    )


def test_deliberate_live_only_builder_diverges() -> None:
    clock = FrozenClock(T0)
    live = LiveState(clock)
    live.apply(_snap(40, 42, at=T0, received=T0))
    inner = InMemoryAsOfStore()
    replay = ClockBoundStore(inner, clock)
    apply_book_update(replay, _snap(40, 42, at=T0, received=T0))
    honest = build_market_view(store=replay, clock=clock, book_keys=BOOK_KEYS)
    forked = _forked_live_builder(store=live, clock=clock, book_keys=BOOK_KEYS)
    assert astuple(honest) != astuple(forked)


_ts = st.integers(min_value=0, max_value=30).map(lambda s: T0 + timedelta(seconds=s))
_px = st.integers(min_value=1, max_value=99)


@given(
    st.lists(
        st.tuples(_ts, _px, _px, st.booleans()),
        min_size=1,
        max_size=12,
    )
)
@settings(max_examples=40)
def test_property_live_replay_agree_every_tick(
    events: list[tuple[datetime, int, int, bool]],
) -> None:
    events_sorted = sorted(events, key=lambda row: row[0])
    clock = FrozenClock(T0)
    live = LiveState(clock)
    inner = InMemoryAsOfStore()
    replay = ClockBoundStore(inner, clock)
    last_received: datetime | None = None
    for received, bid, ask, make_stale in events_sorted:
        if last_received is not None and received < last_received:
            received = last_received
        clock.set(received)
        if make_stale and live.keys():
            update = stale_update(
                venue="kalshi",
                market_id="KXHIGHNY-26JUL04-T90",
                valid_at=received,
                available_at=received,
                source="reconnect",
            )
        else:
            bid_c = min(bid, ask)
            ask_c = max(bid, ask)
            if bid_c == ask_c:
                ask_c = min(99, bid_c + 1)
            update = snapshot_update(
                venue="kalshi",
                market_id="KXHIGHNY-26JUL04-T90",
                valid_at=received,
                available_at=received,
                payload=_payload(bid_c, ask_c),
                source="parity",
            )
        live.apply(update)
        apply_book_update(replay, update)
        last_received = received
        live_err: str | None = None
        replay_err: str | None = None
        live_view = None
        replay_view = None
        try:
            live_view = build_market_view(store=live, clock=clock, book_keys=BOOK_KEYS)
        except StaleBookError as exc:
            live_err = type(exc).__name__
        try:
            replay_view = build_market_view(store=replay, clock=clock, book_keys=BOOK_KEYS)
        except StaleBookError as exc:
            replay_err = type(exc).__name__
        assert live_err == replay_err
        if live_err is None:
            assert live_view is not None and replay_view is not None
            assert astuple(live_view) == astuple(replay_view)


def test_unbound_store_still_refused() -> None:
    clock = FrozenClock(T0)
    with pytest.raises(TypeError, match="unbound store refused"):
        build_market_view(store=InMemoryAsOfStore(), clock=clock, book_keys=())
