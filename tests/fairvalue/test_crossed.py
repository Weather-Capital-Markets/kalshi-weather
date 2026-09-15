"""Crossed-state rate: mechanism only.

These fixtures test that the diagnostic measures what it claims — raw crossing
with no repair, a genuinely independent inverted control, block exclusion.

They cannot and must not stand in for the number. A fixture is built from the
same direction assumption the diagnostic is testing, so a fixture that "passes"
proves nothing about the real sign. The reported rate comes from
``analysis.crossed_rate`` on the pulled corpus, recorded in
``knowledge/data-sources.md`` §1.4.2.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from wxmm.analysis.trades_ingest import RawTrade, parse_trade
from wxmm.fairvalue.crossed import (
    CONFIRM_MAX_RATE,
    MIN_OBSERVATIONS,
    crossed_by_climate_day,
    crossed_diagnostic,
    crossed_state_rate,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _raw(
    *,
    trade_id: str,
    outcome: str,
    yes: str,
    offset_s: int,
    ticker: str = "KXHIGHNY-26AUG12-T90",
    block: bool = False,
) -> RawTrade:
    yes_price = round(float(yes), 4)
    created = (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": "10.00",
            "yes_price_dollars": f"{yes_price:.4f}",
            "no_price_dollars": f"{1.0 - yes_price:.4f}",
            "taker_outcome_side": outcome,
            "taker_book_side": "bid" if outcome == "yes" else "ask",
            "created_time": created,
            "is_block_trade": block,
        },
        source_endpoint="historical",
    )


def _oriented_tape(n_pairs: int, *, crossed_every: int = 0) -> list[RawTrade]:
    """Ask prints above bid prints, with an optional staleness cross injected."""
    out: list[RawTrade] = []
    for i in range(n_pairs):
        cross = crossed_every and (i % crossed_every == 0)
        ask_px = "0.40" if cross else "0.55"
        out.append(_raw(trade_id=f"b{i}", outcome="no", yes="0.45", offset_s=2 * i))
        out.append(_raw(trade_id=f"a{i}", outcome="yes", yes=ask_px, offset_s=2 * i + 1))
    return out


def _run_tape(n_prints: int, *, seed: int = 7) -> list[RawTrade]:
    """Sides chosen pseudo-randomly, so the tape has runs like real tape does."""
    rng = random.Random(seed)
    out: list[RawTrade] = []
    for i in range(n_prints):
        if rng.random() < 0.5:
            price = f"{rng.uniform(0.52, 0.60):.4f}"
            out.append(_raw(trade_id=f"a{i}", outcome="yes", yes=price, offset_s=i))
        else:
            price = f"{rng.uniform(0.40, 0.48):.4f}"
            out.append(_raw(trade_id=f"b{i}", outcome="no", yes=price, offset_s=i))
    return out


def test_measures_raw_crossing_without_repair() -> None:
    """apply_print invalidates the older side; the diagnostic must not."""
    tape = [
        _raw(trade_id="b0", outcome="no", yes="0.60", offset_s=0),
        _raw(trade_id="a0", outcome="yes", yes="0.40", offset_s=1),
        _raw(trade_id="a1", outcome="yes", yes="0.41", offset_s=2),
    ]
    result = crossed_state_rate(tape, sign=1)
    # Both post-bid prints are crossed. Repair would have dropped the stale bid
    # and reported 0, which is exactly the blindness being avoided.
    assert result.n_two_sided == 2
    assert result.n_crossed == 2
    assert result.rate == pytest.approx(1.0)


def test_inverted_replay_is_the_exact_mirror_up_to_ties() -> None:
    """Flipping d swaps the two sides, so the rates must sum to 1 - tie share.

    This is a correctness check on the replay, not corroboration of the sign.
    The inverted rate is arithmetically determined by the as-specified rate and
    carries no independent evidence.
    """
    diag = crossed_diagnostic(_run_tape(600))
    here, there = diag.as_specified.rate, diag.inverted.rate
    tie_share = diag.as_specified.tie_share
    assert here is not None and there is not None and tie_share is not None
    assert here + there == pytest.approx(1.0 - tie_share)
    assert diag.inverted.tie_share == pytest.approx(tie_share)


def test_verdict_needs_a_real_sample() -> None:
    diag = crossed_diagnostic(_oriented_tape(20))
    assert diag.as_specified.n_two_sided < MIN_OBSERVATIONS
    assert diag.verdict == "insufficient_data"


def test_oriented_tape_confirms_and_inverted_tape_refuses() -> None:
    confirmed = crossed_diagnostic(_oriented_tape(MIN_OBSERVATIONS, crossed_every=10))
    assert confirmed.as_specified.rate is not None
    assert confirmed.as_specified.rate <= CONFIRM_MAX_RATE
    assert confirmed.verdict == "sign_confirmed"

    flipped = [
        _raw(
            trade_id=t.trade_id,
            outcome="no" if t.taker_outcome_side == "yes" else "yes",
            yes=str(t.yes_price),
            offset_s=int((t.created_time - T0).total_seconds()),
        )
        for t in _oriented_tape(MIN_OBSERVATIONS, crossed_every=10)
    ]
    assert crossed_diagnostic(flipped).verdict == "sign_inverted"


def test_block_trades_never_print() -> None:
    tape = _oriented_tape(10)
    blocked = tape + [
        _raw(trade_id="blk", outcome="yes", yes="0.01", offset_s=999, block=True)
    ]
    assert crossed_state_rate(tape).n_prints == crossed_state_rate(blocked).n_prints
    assert crossed_diagnostic(blocked).n_block_excluded == 1


def test_per_ticker_spread_is_reported() -> None:
    clean = _oriented_tape(10)
    dirty = [
        _raw(
            trade_id=f"x{i}",
            outcome="no" if i % 2 == 0 else "yes",
            yes="0.60" if i % 2 == 0 else "0.40",
            offset_s=i,
            ticker="KXHIGHNY-26AUG12-T80",
        )
        for i in range(20)
    ]
    result = crossed_state_rate(clean + dirty, sign=1)
    assert result.n_tickers == 2
    assert result.n_tickers_majority_crossed == 1


def test_by_climate_day_buckets() -> None:
    by_day = crossed_by_climate_day(_oriented_tape(10, crossed_every=5))
    assert list(by_day) == ["2026-08-12"]
    assert 0.0 <= by_day["2026-08-12"] <= 1.0
