"""Reconstructed mid vs trade-derived mid.

Built now so it is ready the moment Stage 0 lands. The function always
loads the reconstruction bound first: an INCOMPLETE sweep raises
ReconstructionBoundRequired. A TRADE_DERIVED object is not a substitute.

After COMPLETE, reports mid_reconstructed − mid_trade_derived by staleness
bucket, season, and bracket. Agreement corroborates both; disagreement is
itself a reconstruction-quality measurement independent of the convention
sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from wxmm.fairvalue.anchor_trades import TradeImpliedBook
from wxmm.fairvalue.reconstruction_bound import (
    ReconstructionBound,
    require_reconstruction_bound_for_fit,
)

STALENESS_BUCKETS: tuple[tuple[str, timedelta | None], ...] = (
    ("lt_5m", timedelta(minutes=5)),
    ("lt_1h", timedelta(hours=1)),
    ("lt_6h", timedelta(hours=6)),
    ("ge_6h", None),
)


@dataclass(frozen=True, slots=True)
class MidDiffRow:
    ticker: str
    season: str
    staleness_bucket: str
    mid_reconstructed: Decimal
    mid_trade_derived: Decimal
    diff: Decimal
    bid_staleness: timedelta | None
    ask_staleness: timedelta | None


@dataclass(frozen=True, slots=True)
class MidCompareReport:
    bound: ReconstructionBound
    rows: tuple[MidDiffRow, ...]
    n_compared: int
    n_skipped_missing: int


def _bucket(ask_stale: timedelta | None, bid_stale: timedelta | None) -> str:
    ages = [item for item in (ask_stale, bid_stale) if item is not None]
    if not ages:
        return "unknown"
    age = max(ages)
    if age < timedelta(minutes=5):
        return "lt_5m"
    if age < timedelta(hours=1):
        return "lt_1h"
    if age < timedelta(hours=6):
        return "lt_6h"
    return "ge_6h"


def compare_reconstructed_vs_trade(
    reconstructed_mids: Mapping[str, Decimal | None],
    trade_books: Mapping[str, TradeImpliedBook],
    seasons: Mapping[str, str],
    *,
    bound_path: Path | None = None,
) -> MidCompareReport:
    """Raise until the sweep is COMPLETE. Then differ the two mids."""
    bound = require_reconstruction_bound_for_fit(bound_path)
    rows: list[MidDiffRow] = []
    skipped = 0
    for ticker, recon in reconstructed_mids.items():
        trade = trade_books.get(ticker)
        if recon is None or trade is None or trade.mid is None:
            skipped += 1
            continue
        if trade.provenance != "TRADE_DERIVED":
            raise TypeError("trade_books must be TRADE_DERIVED; provenance is part of the type")
        rows.append(
            MidDiffRow(
                ticker=ticker,
                season=seasons.get(ticker, "unknown"),
                staleness_bucket=_bucket(trade.ask_staleness, trade.bid_staleness),
                mid_reconstructed=recon,
                mid_trade_derived=trade.mid,
                diff=recon - trade.mid,
                bid_staleness=trade.bid_staleness,
                ask_staleness=trade.ask_staleness,
            )
        )
    return MidCompareReport(
        bound=bound,
        rows=tuple(rows),
        n_compared=len(rows),
        n_skipped_missing=skipped,
    )
