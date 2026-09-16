"""Fee decile table on synthetic trade prices."""

from __future__ import annotations

from decimal import Decimal

from wxmm.analysis.fee_deciles import fee_decile_table
from wxmm.analysis.trades_ingest import parse_trade
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee


def _trade(trade_id: str, yes: str, *, block: bool = False) -> object:
    yes_price = round(float(yes), 4)
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": "KXHIGHNY-26AUG12-T90",
            "count_fp": "10.00",
            "yes_price_dollars": f"{yes_price:.4f}",
            "no_price_dollars": f"{1.0 - yes_price:.4f}",
            "taker_outcome_side": "yes",
            "taker_book_side": "ask",
            "created_time": "2026-08-12T12:00:00Z",
            "is_block_trade": block,
        },
        source_endpoint="historical",
    )


def test_fee_decile_table_structure_and_rounding() -> None:
    trades = [_trade(f"t{i}", f"{0.10 + 0.01 * i:.2f}") for i in range(20)]
    table = fee_decile_table(trades, contracts=(1, 100))  # type: ignore[arg-type]
    assert table["n_trades"] == 20
    deciles = table["decile_prices"]
    assert set(deciles) == {f"{i / 10:.1f}" for i in range(1, 10)}
    fees = table["fees_by_contract_and_rounding"]
    assert set(fees) == {"1", "100"}
    for rounding in FeeRounding:
        assert rounding.value in fees["1"]
        q05 = deciles["0.5"]
        expected = str(taker_fee(1, Decimal(str(q05)), rounding).amount)
        assert fees["1"][rounding.value]["0.5"] == expected


def test_fee_decile_table_excludes_block_trades() -> None:
    trades = [_trade("ok", "0.50"), _trade("blk", "0.01", block=True)]
    table = fee_decile_table(trades, contracts=(1,))  # type: ignore[arg-type]
    assert table["n_trades"] == 1
    assert table["decile_prices"]["0.5"] == 0.5
