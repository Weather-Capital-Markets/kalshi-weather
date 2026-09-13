"""Fit β on a TRADE_DERIVED offset. Does not touch the reconstructed gate.

logit(p_hat) = logit(q_trade) + β·x with x from flow and calendar only.
Null β = 0 is pure spread capture against the trade-implied mid.

This path never satisfies ReconstructionBoundRequired. model.fit() is unchanged
and still refuses an INCOMPLETE sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from wxmm.analysis.maker_taker import assert_go_no_go_filled
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.backtest.ledger import Ledger, refuse_unless_preregistered
from wxmm.fairvalue.anchor import apply_logit_adjustment
from wxmm.fairvalue.anchor_trades import (
    ObservedMapping,
    TradeDerivedLadder,
    assert_outcome_bookside_mapping,
    implied_book_from_trades,
    trade_derived_ladder,
)
from wxmm.fairvalue.features import (
    DEFAULT_FLOW_WINDOWS,
    calendar_features,
    flow_features_multiwindow,
)

RIDGE_LAMBDA = 1.0


@dataclass(frozen=True, slots=True)
class TradeDerivedFit:
    provenance: Literal["TRADE_DERIVED"]
    prereg_id: str
    feature_names: tuple[str, ...]
    beta: tuple[float, ...]
    ridge_lambda: float
    n: int
    is_strategy_pnl: Literal[False] = False


def _logit(p: float) -> float:
    import math

    clipped = min(max(p, 1e-9), 1.0 - 1e-9)
    return math.log(clipped / (1.0 - clipped))


def _ridge(x_rows: list[list[float]], y: list[float], lam: float) -> list[float]:
    """(X'X + λI)^{-1} X'y. No pandas. Small n."""
    if not x_rows:
        return []
    n = len(x_rows)
    k = len(x_rows[0])
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        for a in range(k):
            xty[a] += x_rows[i][a] * y[i]
            for b in range(k):
                xtx[a][b] += x_rows[i][a] * x_rows[i][b]
    for a in range(k):
        xtx[a][a] += lam
    return _solve(xtx, xty)


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        diag = aug[col][col]
        if abs(diag) < 1e-12:
            raise ValueError("singular ridge design")
        for j in range(col, n + 1):
            aug[col][j] /= diag
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            for j in range(col, n + 1):
                aug[row][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def _corpus_mapping(trades: Sequence[RawTrade]) -> ObservedMapping | None:
    non_block = [trade for trade in trades if not trade.is_block_trade]
    if not non_block:
        return None
    return assert_outcome_bookside_mapping(non_block)


def _row_features(
    trades: Sequence[RawTrade],
    ticker: str,
    as_of: datetime,
    *,
    mapping: ObservedMapping | None,
) -> dict[str, float]:
    flows = flow_features_multiwindow(trades, as_of=as_of, ticker=ticker, mapping=mapping)
    cal = calendar_features(ticker, as_of)
    out: dict[str, float] = {
        "cal_doy": float(cal.doy),
        "cal_hours_to_close": float(cal.hours_to_close or 0.0),
        "cal_season_DJF": 1.0 if cal.season == "DJF" else 0.0,
        "cal_season_MAM": 1.0 if cal.season == "MAM" else 0.0,
        "cal_season_JJA": 1.0 if cal.season == "JJA" else 0.0,
        "cal_season_SON": 1.0 if cal.season == "SON" else 0.0,
    }
    for name, flow in flows.items():
        raw = flow.as_dict()
        for key, value in raw.items():
            out[f"{name}_{key}"] = 0.0 if value is None else float(value)
    return out


def trade_ladder_or_none(
    trades: Sequence[RawTrade],
    tickers: Sequence[str],
    *,
    as_of: datetime,
    mapping: ObservedMapping | None = None,
) -> TradeDerivedLadder | None:
    verified = mapping if mapping is not None else _corpus_mapping(trades)
    books = {
        ticker: implied_book_from_trades(
            trades, as_of=as_of, ticker=ticker, mapping=verified
        )
        for ticker in tickers
    }
    return trade_derived_ladder(books)


def null_trade_recovery(ladder: TradeDerivedLadder) -> dict[str, Decimal]:
    """β = 0: p_hat = q_trade (renormalised)."""
    q = {ticker: book.mid for ticker, book in ladder.books.items() if book.mid is not None}
    if len(q) != len(ladder.books):
        raise ValueError("null recovery refused: incomplete TRADE_DERIVED ladder")
    total = sum(q.values(), Decimal("0"))
    if total <= 0:
        raise ValueError("null recovery refused: non-positive mass")
    normalised = {k: v / total for k, v in q.items()}
    zero = {k: Decimal("0") for k in normalised}
    return apply_logit_adjustment(normalised, zero)


def fit_trade_derived(
    trades: Sequence[RawTrade],
    *,
    tickers: Sequence[str],
    as_of: datetime,
    yes_won: Mapping[str, bool],
    prereg: Mapping[str, Any],
    prereg_dir: Path,
    ledger: Ledger,
    ridge_lambda: float = RIDGE_LAMBDA,
) -> TradeDerivedFit:
    """Offset-logit ridge on flow+calendar. Never opens the reconstructed gate."""
    registered = refuse_unless_preregistered(dict(prereg), prereg_dir)
    assert_go_no_go_filled(registered)
    mapping = _corpus_mapping(trades)
    ladder = trade_ladder_or_none(trades, tickers, as_of=as_of, mapping=mapping)
    if ladder is None:
        raise ValueError("TRADE_DERIVED ladder incomplete; provider returns None")
    names: list[str] | None = None
    x_rows: list[list[float]] = []
    y: list[float] = []
    for ticker in tickers:
        book = ladder.books[ticker]
        if book.mid is None or ticker not in yes_won:
            continue
        q = float(book.mid)
        residual = (1.0 if yes_won[ticker] else 0.0) - q
        working = residual / max(q * (1.0 - q), 1e-6)
        feats = _row_features(trades, ticker, as_of, mapping=mapping)
        if names is None:
            names = sorted(feats)
        x_rows.append([feats[name] for name in names])
        y.append(working)
    if names is None or not x_rows:
        raise ValueError("no complete TRADE_DERIVED rows to fit")
    beta = _ridge(x_rows, y, ridge_lambda)
    result = TradeDerivedFit(
        provenance="TRADE_DERIVED",
        prereg_id=str(registered["prereg_id"]),
        feature_names=tuple(names),
        beta=tuple(beta),
        ridge_lambda=ridge_lambda,
        n=len(x_rows),
    )
    ledger.record(
        "C1_M1_TRADE_DERIVED_FIT",
        {
            "prereg_id": result.prereg_id,
            "n": result.n,
            "provenance": "TRADE_DERIVED",
            "is_strategy_pnl": False,
        },
    )
    return result


def default_windows() -> tuple[timedelta, ...]:
    return DEFAULT_FLOW_WINDOWS
