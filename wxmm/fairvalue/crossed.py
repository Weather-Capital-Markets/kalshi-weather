"""Crossed-state rate: the only independent check on the direction sign.

``taker_book_side`` is a deterministic function of ``taker_outcome_side`` —
the observed cross-tab has an exactly-zero off-diagonal — so it carries no
information about direction and cannot corroborate d. The cross-tab is a
consistency gate, not a second source.

What is left is a property of the *prices*, which no label field manufactured.
Replay prints into a naive two-sided book. If d is right, ask prints sit above
bid prints most of the time and ``ask < bid`` is occasional staleness. If d is
inverted, the anchor is crossed nearly always and the rate approaches 1.

The evidence is the level of the as-specified rate against the 0.5 coin flip,
not the comparison with the inverted replay. Flipping d swaps which side each
print writes, so ``crossed(-d)`` is the exact complement of ``crossed(+d)``
except where ``ask == bid``. The inverted replay is therefore a readability
aid, not a second source; ``rate + inverted.rate == 1 - tie_share`` is an
identity and is asserted as a correctness check on the replay.

This cannot be settled by a synthetic fixture, because the fixture would be
built from the assumption under test. Run it on the real corpus.

Allowed to assume
    Direction is ``yes`` → d=+1 (ask print), ``no`` → d=−1 (bid print).
    ``created_time`` orders the replay.

Must never
    Invalidate the older side before measuring — invalidation is the repair
    the diagnostic is trying to observe. Drop block-trade exclusion. Report
    only the pooled rate and hide the per-ticker spread. Claim the cross-tab
    verified the sign. Present the inverted replay as corroboration.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from wxmm.analysis.trades_ingest import RawTrade
from wxmm.fairvalue.anchor_trades import yes_space_print

Sign = Literal[1, -1]

# Preregistered thresholds. A sign that orients the book must put the crossed
# rate well under the 0.5 coin flip; an inverted sign drives it toward 1.0.
CONFIRM_MAX_RATE = 0.35
INVERT_MIN_RATE = 0.65
MIN_OBSERVATIONS = 1000

Verdict = Literal["sign_confirmed", "sign_inverted", "indeterminate", "insufficient_data"]


@dataclass(frozen=True, slots=True)
class CrossedRate:
    """One replay hypothesis. ``rate`` is the headline number."""

    sign: Sign
    n_prints: int
    n_two_sided: int
    n_crossed: int
    n_touching: int
    rate: float | None
    mean_gap_cents: float | None
    mean_uncrossed_gap_cents: float | None
    median_gap_cents: float | None
    n_tickers: int
    n_tickers_two_sided: int
    n_tickers_majority_crossed: int
    median_ticker_rate: float | None

    @property
    def tie_share(self) -> float | None:
        if not self.n_two_sided:
            return None
        return self.n_touching / self.n_two_sided

    @property
    def summary(self) -> str:
        if self.rate is None:
            return f"sign={self.sign:+d} no two-sided observations"
        return (
            f"sign={self.sign:+d} crossed={self.rate:.4f} "
            f"({self.n_crossed}/{self.n_two_sided}) "
            f"median_gap={self.median_gap_cents:.2f}c "
            f"ties={self.n_touching}"
        )


@dataclass(frozen=True, slots=True)
class CrossedDiagnostic:
    as_specified: CrossedRate
    inverted: CrossedRate
    verdict: Verdict
    note: str
    n_block_excluded: int
    is_strategy_pnl: Literal[False] = False


def _direction_for(outcome: str, sign: Sign) -> int:
    base = 1 if outcome == "yes" else -1
    return base * sign


def _gap_cents(ask: Decimal, bid: Decimal) -> int:
    return int(((ask - bid) * Decimal("100")).to_integral_value())


def _median_from_histogram(hist: Counter[int]) -> float | None:
    total = sum(hist.values())
    if total == 0:
        return None
    target = total / 2.0
    seen = 0
    for key in sorted(hist):
        seen += hist[key]
        if seen >= target:
            return float(key)
    return None


class CrossedAccumulator:
    """Shard-safe replay. A ticker lives on one climate day, hence one month shard."""

    def __init__(self, *, sign: Sign) -> None:
        self.sign: Sign = sign
        self.n_prints = 0
        self.n_two_sided = 0
        self.n_crossed = 0
        self.n_touching = 0
        self.n_tickers = 0
        self.n_block_excluded = 0
        self.gap_hist: Counter[int] = Counter()
        self.uncrossed_gap_sum = 0
        self.n_uncrossed = 0
        self.ticker_rates: list[float] = []
        self.n_majority = 0

    def add(self, trades: Sequence[RawTrade]) -> None:
        by_ticker: dict[str, list[RawTrade]] = {}
        for trade in trades:
            if trade.is_block_trade:
                self.n_block_excluded += 1
                continue
            by_ticker.setdefault(trade.ticker, []).append(trade)
        self.n_tickers += len(by_ticker)
        for ticker_trades in by_ticker.values():
            ordered = sorted(ticker_trades, key=lambda t: (t.created_time, t.trade_id))
            bid: Decimal | None = None
            ask: Decimal | None = None
            local_two_sided = 0
            local_crossed = 0
            for trade in ordered:
                price = yes_space_print(trade).yes_price
                if _direction_for(trade.taker_outcome_side, self.sign) == 1:
                    ask = price
                else:
                    bid = price
                self.n_prints += 1
                if bid is None or ask is None:
                    continue
                local_two_sided += 1
                self.gap_hist[_gap_cents(ask, bid)] += 1
                if ask < bid:
                    local_crossed += 1
                elif ask == bid:
                    self.n_touching += 1
                else:
                    self.n_uncrossed += 1
                    self.uncrossed_gap_sum += _gap_cents(ask, bid)
            self.n_two_sided += local_two_sided
            self.n_crossed += local_crossed
            if local_two_sided:
                rate = local_crossed / local_two_sided
                self.ticker_rates.append(rate)
                if rate > 0.5:
                    self.n_majority += 1

    def finish(self) -> CrossedRate:
        rates = sorted(self.ticker_rates)
        median_ticker = rates[len(rates) // 2] if rates else None
        mean_gap = (
            sum(key * count for key, count in self.gap_hist.items()) / sum(self.gap_hist.values())
            if self.gap_hist
            else None
        )
        mean_uncrossed = (
            (self.uncrossed_gap_sum / self.n_uncrossed) if self.n_uncrossed else None
        )
        return CrossedRate(
            sign=self.sign,
            n_prints=self.n_prints,
            n_two_sided=self.n_two_sided,
            n_crossed=self.n_crossed,
            n_touching=self.n_touching,
            rate=(self.n_crossed / self.n_two_sided) if self.n_two_sided else None,
            mean_gap_cents=mean_gap,
            mean_uncrossed_gap_cents=mean_uncrossed,
            median_gap_cents=_median_from_histogram(self.gap_hist),
            n_tickers=self.n_tickers,
            n_tickers_two_sided=len(self.ticker_rates),
            n_tickers_majority_crossed=self.n_majority,
            median_ticker_rate=median_ticker,
        )


def crossed_state_rate(
    trades: Sequence[RawTrade],
    *,
    sign: Sign = 1,
) -> CrossedRate:
    """Naive replay with no crossed-state repair. Block trades never print."""
    acc = CrossedAccumulator(sign=sign)
    acc.add(trades)
    return acc.finish()


def crossed_diagnostic_from_shards(shards: Sequence[Sequence[RawTrade]]) -> CrossedDiagnostic:
    """Same diagnostic as ``crossed_diagnostic`` without concatenating shards."""
    specified = CrossedAccumulator(sign=1)
    inverted = CrossedAccumulator(sign=-1)
    for shard in shards:
        specified.add(shard)
        inverted.add(shard)
    as_specified = specified.finish()
    inv = inverted.finish()
    verdict, note = _verdict(as_specified, inv)
    return CrossedDiagnostic(
        as_specified=as_specified,
        inverted=inv,
        verdict=verdict,
        note=note,
        n_block_excluded=specified.n_block_excluded,
    )


def _verdict(as_specified: CrossedRate, inverted: CrossedRate) -> tuple[Verdict, str]:
    if (
        as_specified.rate is None
        or inverted.rate is None
        or as_specified.n_two_sided < MIN_OBSERVATIONS
    ):
        return (
            "insufficient_data",
            f"need >= {MIN_OBSERVATIONS} two-sided observations, "
            f"have {as_specified.n_two_sided}",
        )
    # The decision reads the level of the as-specified rate. The inverted rate
    # is its complement up to ties and adds no information to the test.
    here = as_specified.rate
    gap = as_specified.median_gap_cents
    if here <= CONFIRM_MAX_RATE:
        return (
            "sign_confirmed",
            f"as-specified crossed {here:.4f} <= {CONFIRM_MAX_RATE}; ask sits "
            f"above bid (median implied spread {gap:+.1f}c), crossing is staleness",
        )
    if here >= INVERT_MIN_RATE:
        return (
            "sign_inverted",
            f"as-specified crossed {here:.4f} >= {INVERT_MIN_RATE} (median implied "
            f"spread {gap:+.1f}c); d is backwards, do not fit",
        )
    return (
        "indeterminate",
        f"as-specified crossed {here:.4f} sits between {CONFIRM_MAX_RATE} and "
        f"{INVERT_MIN_RATE}; the anchor is not oriented either way",
    )


def hard_stop_inverted(diag: CrossedDiagnostic) -> bool:
    """Preregistered halt when the direction sign is clearly backwards."""
    rate = diag.as_specified.rate
    return diag.verdict == "sign_inverted" or (rate is not None and rate > 0.50)


def crossed_diagnostic(trades: Sequence[RawTrade]) -> CrossedDiagnostic:
    """Paired replay under d and −d. Report before anything is fitted."""
    return crossed_diagnostic_from_shards((trades,))


def crossed_by_climate_day(
    trades: Sequence[RawTrade],
) -> Mapping[str, float]:
    """Per-climate-day crossed rate under the as-specified sign. Reported, not gated."""
    buckets: dict[str, list[RawTrade]] = {}
    for trade in trades:
        buckets.setdefault(trade.climate_day.isoformat(), []).append(trade)
    out: dict[str, float] = {}
    for day, rows in buckets.items():
        result = crossed_state_rate(rows, sign=1)
        if result.rate is not None:
            out[day] = result.rate
    return dict(sorted(out.items()))


def first_and_last_print(trades: Sequence[RawTrade]) -> tuple[datetime, datetime] | None:
    if not trades:
        return None
    times = [t.created_time for t in trades]
    return min(times), max(times)
