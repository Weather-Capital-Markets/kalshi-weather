"""TRADE_DERIVED FairValueProvider. Structural Protocol; no execute, no send.

β = 0 still returns the normalised trade-implied ladder so a null model
can generate shadow fill and mark-out data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from wxmm.analysis.trades_ingest import RawTrade
from wxmm.fairvalue.anchor_trades import ObservedMapping, filter_trades_as_of
from wxmm.fairvalue.model_trades import (
    _row_features,
    null_trade_recovery,
    trade_ladder_or_none,
)
from wxmm.fairvalue.v0_min import predict_adjusted
from wxmm.strategy.view import MarketView

MarketId = str
Probability = str


@dataclass(frozen=True, slots=True)
class TradeDerivedFairValue:
    """Injected β + as-of trades. Returns None when the ladder is incomplete."""

    feature_names: tuple[str, ...]
    beta: tuple[float, ...]
    trades: tuple[RawTrade, ...]
    as_of: datetime
    mapping: ObservedMapping | None = None
    feat_mean: tuple[float, ...] | None = None
    feat_std: tuple[float, ...] | None = None

    def fair(self, view: MarketView) -> Mapping[MarketId, Probability] | None:
        tickers = [book.market_id for book in view.books]
        if not tickers:
            return None
        as_of_trades = filter_trades_as_of(self.trades, self.as_of)
        ladder = trade_ladder_or_none(
            as_of_trades, tickers, as_of=self.as_of, mapping=self.mapping
        )
        if ladder is None:
            return None
        q_map = null_trade_recovery(ladder)
        if not self.beta or all(value == 0.0 for value in self.beta):
            return {key: format(value, "f") for key, value in q_map.items()}
        x_rows: list[list[float]] = []
        for ticker in tickers:
            feats = _row_features(
                as_of_trades, ticker, self.as_of, mapping=self.mapping
            )
            x_rows.append([float(feats.get(name, 0.0)) for name in self.feature_names])
        if self.feat_mean is not None and self.feat_std is not None:
            scaled: list[list[float]] = []
            for row in x_rows:
                scaled.append(
                    [
                        (row[i] - self.feat_mean[i])
                        / (self.feat_std[i] if self.feat_std[i] > 1e-12 else 1.0)
                        for i in range(len(self.feature_names))
                    ]
                )
            x_use: Sequence[Sequence[float]] = scaled
        else:
            x_use = x_rows
        adjusted = predict_adjusted(q_map, tickers, x_use, self.beta)
        return {key: format(value, "f") for key, value in adjusted.items()}


def null_trade_fair_value(
    trades: Sequence[RawTrade],
    as_of: datetime,
    *,
    mapping: ObservedMapping | None = None,
) -> TradeDerivedFairValue:
    """β = 0 provider. Still a FairValueProvider; still no capital."""
    return TradeDerivedFairValue(
        feature_names=(),
        beta=(),
        trades=tuple(trades),
        as_of=as_of,
        mapping=mapping,
    )
