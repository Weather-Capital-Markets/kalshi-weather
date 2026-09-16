"""M/A from trades uses mean, not median."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl

from analysis.trade_turnover import m_and_a
from wxmm.analysis.trades_ingest import RawTrade, raw_trade_to_record


def _row(
    trade_id: str,
    climate: str,
    yes: str,
    count: str,
) -> dict[str, object]:
    trade = RawTrade(
        trade_id=trade_id,
        ticker="KXHIGHNY-23JAN01-T90",
        count=Decimal(count),
        yes_price=Decimal(yes),
        no_price=Decimal("1") - Decimal(yes),
        taker_outcome_side="yes",
        taker_book_side="ask",
        created_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
        is_block_trade=False,
        source_endpoint="historical",
        climate_day=date.fromisoformat(climate),
        ladder_regime="six_bracket",
        settlement_rule_id="kalshi_first_10am_et_thru_2024-09-03",
        close_time_convention="lst_1159",
    )
    return raw_trade_to_record(trade)


def test_m_is_mean_not_median() -> None:
    rows = [
        _row("a", "2023-01-01", "0.50", "100"),  # premium 50
        _row("b", "2023-01-02", "0.50", "100"),  # 50
        _row("c", "2023-01-03", "0.50", "400"),  # 200
    ]
    frame = pl.DataFrame(rows)
    payload = m_and_a(frame)
    assert payload["used_median_for_mean"] is False
    # daily premiums 50, 50, 200 → mean 100, median 50
    assert payload["M"]["mean_daily_premium"] == 100.0
    assert payload["A"]["average_trade_price_in_band"] == 0.5
