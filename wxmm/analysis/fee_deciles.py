"""Fee sensitivity at observed trade-price deciles.

Samples YES prices from non-block trades and reports Kalshi taker fees at
each decile for a small contract grid and both rounding policies.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import numpy as np

from wxmm.analysis.trades_ingest import RawTrade
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee

_DECILE_QUANTILES: tuple[float, ...] = tuple(i / 10 for i in range(1, 10))


def fee_decile_table(
    trades: Sequence[RawTrade],
    contracts: tuple[int, ...] = (1, 100, 500),
) -> dict[str, object]:
    """Return decile prices and fees at each (contracts, rounding) pair."""
    prices = [float(trade.yes_price) for trade in trades if not trade.is_block_trade]
    decile_prices: dict[str, float] = {}
    if prices:
        arr = np.asarray(prices, dtype=float)
        for q in _DECILE_QUANTILES:
            decile_prices[f"{q:.1f}"] = float(np.quantile(arr, q))

    fees: dict[str, dict[str, dict[str, str]]] = {}
    for count in contracts:
        count_key = str(count)
        fees[count_key] = {}
        for rounding in FeeRounding:
            rounding_key = rounding.value
            fees[count_key][rounding_key] = {}
            for q_label, price in decile_prices.items():
                fee = taker_fee(count, Decimal(str(price)), rounding)
                fees[count_key][rounding_key][q_label] = str(fee.amount)

    return {
        "n_trades": len(prices),
        "decile_prices": decile_prices,
        "fees_by_contract_and_rounding": fees,
    }
