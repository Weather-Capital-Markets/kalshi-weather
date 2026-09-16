"""Market-implied ladder from ``MarketView``. Train and deploy on mid.

Allowed to assume
    ``MarketView`` books are KXHIGHNY bracket tickers with bid/ask in cents.
    Missing quotes stay ``None``; never imputed.

Must never
    Impute a one-sided or absent bracket. Use microprice in ``p_hat``.
    Read the book differently from ``decide/engine.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from math import log
from typing import Mapping

from wxmm.strategy.view import BookView, MarketView

_EPS = Decimal("1e-12")
_MIN_P = Decimal("1e-9")
_MAX_P = Decimal("1") - _MIN_P


@dataclass(frozen=True, slots=True)
class BracketQuote:
    market_id: str
    bid_cents: int | None
    ask_cents: int | None
    mid_cents: int | None
    implied_prob: Decimal | None
    ask_implied_prob: Decimal | None
    two_sided: bool
    staleness: timedelta


@dataclass(frozen=True, slots=True)
class LadderQuote:
    brackets: tuple[BracketQuote, ...]

    def implied_probs(self) -> dict[str, Decimal | None]:
        return {row.market_id: row.implied_prob for row in self.brackets}

    def ask_probs(self) -> dict[str, Decimal | None]:
        return {row.market_id: row.ask_implied_prob for row in self.brackets}


def _mid_cents(book: BookView) -> int | None:
    if book.bid_cents is None or book.ask_cents is None:
        return None
    return (book.bid_cents + book.ask_cents) // 2


def _prob_from_cents(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return Decimal(cents) / Decimal(100)


def market_implied(view: MarketView) -> LadderQuote:
    """Per-bracket bid/ask/mid and raw implied probabilities from mid."""
    rows: list[BracketQuote] = []
    for book in view.books:
        mid = _mid_cents(book) if book.two_sided else None
        rows.append(
            BracketQuote(
                market_id=book.market_id,
                bid_cents=book.bid_cents,
                ask_cents=book.ask_cents,
                mid_cents=mid,
                implied_prob=_prob_from_cents(mid),
                ask_implied_prob=_prob_from_cents(book.ask_cents),
                two_sided=book.two_sided,
                staleness=book.staleness,
            )
        )
    return LadderQuote(brackets=tuple(rows))


def coherence_residual(view: MarketView) -> Decimal | None:
    """``Σ q_k − 1`` on executable ask prices (buy YES), never mids.

    Returns ``None`` when any bracket lacks an ask.
    """
    ladder = market_implied(view)
    asks = [row.ask_implied_prob for row in ladder.brackets]
    if not asks or any(item is None for item in asks):
        return None
    total = sum((item for item in asks if item is not None), Decimal(0))
    return total - Decimal(1)


def ladder_complete(ladder: LadderQuote) -> bool:
    return all(row.implied_prob is not None for row in ladder.brackets)


def normalised_market_ladder(ladder: LadderQuote) -> dict[str, Decimal]:
    """Normalise mid-implied probabilities to sum to 1.

    Raises ``ValueError`` if any bracket is missing or the sum is zero.
    """
    raw = ladder.implied_probs()
    probs: dict[str, Decimal] = {}
    for key, value in raw.items():
        if value is None:
            raise ValueError("ladder incomplete: missing implied_prob on one or more brackets")
        probs[key] = value
    total = sum(probs.values(), Decimal(0))
    if total <= _EPS:
        raise ValueError("ladder normalisation: sum of implied probabilities is zero")
    return {key: value / total for key, value in probs.items()}


def _logit(p: Decimal) -> Decimal:
    pf = float(p)
    if pf <= 0.0 or pf >= 1.0:
        raise ValueError(f"logit undefined for p={p}")
    return Decimal(str(log(pf / (1.0 - pf))))


def _inv_logit(x: Decimal) -> Decimal:
    import math

    # Clip to avoid OverflowError on large β·x; ±60 is 0/1 to machine precision.
    ex = min(max(float(x), -60.0), 60.0)
    p = 1.0 / (1.0 + math.exp(-ex))
    return Decimal(str(p))


def apply_logit_adjustment(
    q_normalised: Mapping[str, Decimal],
    adjustments: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """``logit(p_k) = logit(q_k) + adj_k`` then renormalise.

    ``adjustments`` all zero recovers ``q_normalised`` (null recovery).
    """
    if set(q_normalised.keys()) != set(adjustments.keys()):
        raise ValueError("adjustment keys must match ladder keys")
    unnorm: dict[str, Decimal] = {}
    for key, q in q_normalised.items():
        adj = adjustments[key]
        if adj == 0:
            unnorm[key] = q
        else:
            unnorm[key] = _inv_logit(_logit(q) + adj)
    total = sum(unnorm.values(), Decimal(0))
    if total <= _EPS:
        raise ValueError("adjustment produced non-positive mass")
    return {key: value / total for key, value in unnorm.items()}


def null_recovery_probs(ladder: LadderQuote) -> dict[str, Decimal]:
    """``beta = 0``: adjusted ladder equals normalised market mids."""
    q = normalised_market_ladder(ladder)
    zero = {key: Decimal(0) for key in q}
    return apply_logit_adjustment(q, zero)
