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
    filter_trades_as_of,
    implied_book_from_trades,
    trade_derived_ladder,
)
from wxmm.fairvalue.features import (
    DEFAULT_FLOW_WINDOWS,
    STALENESS_KEYS,
    WINDOW_INVARIANT_KEYS,
    calendar_features,
    flow_features_multiwindow,
)

RIDGE_LAMBDA = 1.0
DESIGN_DROPPED_KEYS: frozenset[str] = frozenset({"trade_count"})
"""``intensity_per_hour`` is ``trade_count`` over a constant, so within a window
the two are the same column once standardised. Keep the intensity, which is
comparable across windows, and keep the count out of the design."""
# Design-matrix prefix for as-of state. Renamed off ``book_`` in a later commit.
INVARIANT_FEATURE_PREFIX = "book_"
GAP_SINCE_LAST_DECISION = (
    "deferred: gap_since_last_seconds stays imputed to 0.0 because no "
    "locked-snapshot fit produced per-feature counts"
)
STALENESS_POLICY = (
    "rows with a missing staleness key are excluded from the fit; "
    "those keys are never imputed"
)


class ImputationTally:
    """None→0.0 cells on rows that enter a fit. Staleness keys raise."""

    def __init__(self) -> None:
        self.imputed: dict[str, int] = {}
        self.rows_excluded_missing_staleness: int = 0

    def note_imputed(self, feature: str, n: int = 1) -> None:
        if _is_staleness_feature(feature):
            raise AssertionError(
                f"staleness key {feature!r} imputed into a row that enters the fit"
            )
        if n:
            self.imputed[feature] = self.imputed.get(feature, 0) + n

    def absorb_imputed(self, other: ImputationTally) -> None:
        for feature, count in other.imputed.items():
            self.note_imputed(feature, count)

    def as_dict(self) -> dict[str, object]:
        return {
            "imputed_cells": {key: self.imputed[key] for key in sorted(self.imputed)},
            "rows_excluded_missing_staleness": self.rows_excluded_missing_staleness,
            "gap_since_last_decision": GAP_SINCE_LAST_DECISION,
            "staleness_policy": STALENESS_POLICY,
        }


def _is_staleness_feature(feature: str) -> bool:
    if feature in STALENESS_KEYS:
        return True
    return any(
        feature == f"{INVARIANT_FEATURE_PREFIX}{key}" or feature.endswith(f"_{key}")
        for key in STALENESS_KEYS
    )


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
    tally: ImputationTally | None = None,
) -> dict[str, float] | None:
    """Flow and calendar columns for one ticker.

    A missing staleness key excludes the row. Other missing flow values,
    including ``gap_since_last_seconds``, are still filled with 0.0. The gap
    fill is recorded and left in place: no locked-snapshot fit has produced
    the counts that would decide it.
    """
    as_of_trades = filter_trades_as_of(trades, as_of)
    flows = flow_features_multiwindow(
        as_of_trades, as_of=as_of, ticker=ticker, mapping=mapping
    )
    cal = calendar_features(ticker, as_of)
    out: dict[str, float] = {
        "cal_doy_sin": float(cal.doy_sin),
        "cal_doy_cos": float(cal.doy_cos),
        "cal_hours_to_close": float(cal.hours_to_close or 0.0),
        "cal_season_DJF": 1.0 if cal.season == "DJF" else 0.0,
        "cal_season_MAM": 1.0 if cal.season == "MAM" else 0.0,
        "cal_season_JJA": 1.0 if cal.season == "JJA" else 0.0,
        "cal_season_SON": 1.0 if cal.season == "SON" else 0.0,
    }
    pending: dict[str, int] = {}
    missing_staleness = False
    for name, flow in flows.items():
        if missing_staleness:
            break
        raw = flow.as_dict()
        for key, value in raw.items():
            if key in DESIGN_DROPPED_KEYS:
                continue
            column = (
                f"{INVARIANT_FEATURE_PREFIX}{key}"
                if key in WINDOW_INVARIANT_KEYS
                else f"{name}_{key}"
            )
            if key in WINDOW_INVARIANT_KEYS and column in out:
                continue
            if value is None and key in STALENESS_KEYS:
                missing_staleness = True
                break
            if value is None:
                if _is_staleness_feature(key):
                    raise AssertionError(
                        f"staleness key {key!r} imputed into a row that enters the fit"
                    )
                numeric = 0.0
                pending[column] = pending.get(column, 0) + 1
            else:
                numeric = float(value)
            out[column] = numeric
    staleness_present = all(
        f"{INVARIANT_FEATURE_PREFIX}{key}" in out for key in STALENESS_KEYS
    )
    if missing_staleness or not staleness_present:
        if tally is not None:
            tally.rows_excluded_missing_staleness += 1
        return None
    if tally is not None:
        for column, count in pending.items():
            tally.note_imputed(column, count)
    return out


def trade_ladder_or_none(
    trades: Sequence[RawTrade],
    tickers: Sequence[str],
    *,
    as_of: datetime,
    mapping: ObservedMapping | None = None,
) -> TradeDerivedLadder | None:
    as_of_trades = filter_trades_as_of(trades, as_of)
    verified = mapping if mapping is not None else _corpus_mapping(as_of_trades)
    books = {
        ticker: implied_book_from_trades(
            as_of_trades, as_of=as_of, ticker=ticker, mapping=verified
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
    tally = ImputationTally()
    for ticker in tickers:
        book = ladder.books[ticker]
        if book.mid is None or ticker not in yes_won:
            continue
        q = float(book.mid)
        residual = (1.0 if yes_won[ticker] else 0.0) - q
        working = residual / max(q * (1.0 - q), 1e-6)
        feats = _row_features(trades, ticker, as_of, mapping=mapping, tally=tally)
        if feats is None:
            continue
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
            "imputation": tally.as_dict(),
            "is_strategy_pnl": False,
        },
    )
    return result


def default_windows() -> tuple[timedelta, ...]:
    return DEFAULT_FLOW_WINDOWS
