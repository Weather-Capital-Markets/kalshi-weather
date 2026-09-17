"""Complement census: measure yes+no violations without raising."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from wxmm.analysis.complement import complement_census
from wxmm.analysis.trades_ingest import RawTrade, parse_trade


def _clean_trade(*, trade_id: str = "t1", yes: str = "0.56") -> RawTrade:
    yes_price = float(yes)
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": "KXHIGHNY-26AUG12-T90",
            "count_fp": "10.00",
            "yes_price_dollars": f"{yes_price:.4f}",
            "no_price_dollars": f"{1.0 - yes_price:.4f}",
            "taker_outcome_side": "yes",
            "taker_book_side": "bid",
            "created_time": "2026-08-12T15:00:00Z",
            "is_block_trade": False,
        },
        source_endpoint="historical",
    )


def _bad_trade(clean: RawTrade) -> RawTrade:
    return replace(clean, yes_price=Decimal("0.56"), no_price=Decimal("0.56"))


def test_clean_trades_zero_violations() -> None:
    trades = [_clean_trade(trade_id=f"t{i}", yes=f"0.{50 + i}") for i in range(3)]
    census = complement_census(trades)
    assert census.n_trades == 3
    assert census.n_violations == 0
    assert census.worst_abs_deviation is None
    assert census.worst_trade_id is None
    assert census.tolerance == 0.0001


def test_one_bad_trade_reports_worst_deviation() -> None:
    clean = _clean_trade(trade_id="good")
    bad = _bad_trade(clean)
    census = complement_census([clean, bad, _clean_trade(trade_id="good2", yes="0.45")])
    assert census.n_trades == 3
    assert census.n_violations == 1
    assert census.worst_trade_id == clean.trade_id
    assert census.worst_abs_deviation is not None
    assert abs(census.worst_abs_deviation - 0.12) < 1e-9
