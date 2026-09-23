"""Price-complement census on RawTrade sequences.

Counts ``yes_price + no_price`` violations without raising. Parsing and parquet
load paths assert complement; this module measures how often the pulled corpus
would fail that gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from wxmm.analysis.trades_ingest import PRICE_COMPLEMENT_TOLERANCE, RawTrade


@dataclass(frozen=True, slots=True)
class ComplementCensus:
    n_trades: int
    n_violations: int
    worst_abs_deviation: float | None  # max |yes+no-1|
    worst_trade_id: str | None
    tolerance: float  # PRICE_COMPLEMENT_TOLERANCE as float


def _deviation(yes_price: float, no_price: float) -> float:
    return abs(yes_price + no_price - 1.0)


def complement_census(trades: Sequence[RawTrade]) -> ComplementCensus:
    """Scan in-memory trades; never raises on complement violations."""
    tolerance = float(PRICE_COMPLEMENT_TOLERANCE)
    n_violations = 0
    worst_abs_deviation: float | None = None
    worst_trade_id: str | None = None

    for trade in trades:
        dev = _deviation(float(trade.yes_price), float(trade.no_price))
        if dev > tolerance:
            n_violations += 1
            if worst_abs_deviation is None or dev > worst_abs_deviation:
                worst_abs_deviation = dev
                worst_trade_id = trade.trade_id

    return ComplementCensus(
        n_trades=len(trades),
        n_violations=n_violations,
        worst_abs_deviation=worst_abs_deviation,
        worst_trade_id=worst_trade_id,
        tolerance=tolerance,
    )


def complement_census_parquet(path: Path) -> ComplementCensus:
    """Polars scan of parquet file or shard directory; no RawTrade materialization."""
    target = Path(path)
    tolerance = float(PRICE_COMPLEMENT_TOLERANCE)
    pattern = str(target / "*.parquet") if target.is_dir() else str(target)
    lf = pl.scan_parquet(pattern).select(
        pl.col("trade_id").cast(pl.Utf8),
        pl.col("yes_price").cast(pl.Float64, strict=False),
        pl.col("no_price").cast(pl.Float64, strict=False),
    )
    lf = lf.with_columns(
        deviation=(pl.col("yes_price") + pl.col("no_price") - 1.0).abs()
    )
    stats = lf.select(
        pl.len().alias("n_trades"),
        (pl.col("deviation") > tolerance).sum().alias("n_violations"),
    ).collect()
    n_trades = int(stats["n_trades"][0])
    n_violations = int(stats["n_violations"][0])

    worst_abs_deviation: float | None = None
    worst_trade_id: str | None = None
    if n_violations:
        worst = (
            lf.filter(pl.col("deviation") > tolerance)
            .sort("deviation", descending=True)
            .select("trade_id", "deviation")
            .limit(1)
            .collect()
        )
        worst_abs_deviation = float(worst["deviation"][0])
        worst_trade_id = str(worst["trade_id"][0])

    return ComplementCensus(
        n_trades=n_trades,
        n_violations=n_violations,
        worst_abs_deviation=worst_abs_deviation,
        worst_trade_id=worst_trade_id,
        tolerance=tolerance,
    )
