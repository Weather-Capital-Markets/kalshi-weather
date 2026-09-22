"""C1-M2 realised half-spread on the trade tape.

Retention is cents per contract. It is not converted to a percentage.

The print being marked is excluded from the pre-trade anchor. A future mark
at ``t+h`` uses only prints with ``created_time <= t+h``. The strict touch
mid exists only when ``ask > bid`` (ties excluded). The second estimator is
the previous YES print.

Must never
    Report a percentage without a denominator in the same sentence.
    Include the marked print in its own pre-trade mid.
    Use a print after ``t+h`` in ``m_{t+h}``.
    Treat ``ask == bid`` as an uncrossed touch.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Sequence

from wxmm.core.utc import require_utc

MakerSide = Literal["buy", "sell"]
MM_PROGRAM_START = date(2024, 3, 11)
# Carried from the C1-M2 addendum. Not re-derived here.
GATE0_REQUIRED_E_TIMES_S = 0.0016


def maker_retention_cents(*, side: MakerSide, price: Decimal, mark: Decimal) -> Decimal:
    """Cents per contract. Buy wants the mark up; sell wants it down."""
    if side == "buy":
        return (mark - price) * Decimal("100")
    if side == "sell":
        return (price - mark) * Decimal("100")
    raise ValueError(f"side must be buy|sell, not {side!r}")


def strict_uncrossed_mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    """Touch mid only when ask > bid. Ties and crosses are missing."""
    if bid is None or ask is None:
        return None
    if ask > bid:
        return (ask + bid) / Decimal("2")
    return None


@dataclass(frozen=True, slots=True)
class TapePrint:
    ts: datetime
    yes_price: Decimal
    direction: Literal[1, -1]
    count: Decimal
    trade_id: str


@dataclass(frozen=True, slots=True)
class MarkedFill:
    """One maker fill with anchors that exclude the print itself."""

    trade_id: str
    ts: datetime
    yes_price: Decimal
    count: Decimal
    maker_side: MakerSide
    pre_strict_mid: Decimal | None
    pre_prior_print: Decimal | None
    mark_strict_mid: Decimal | None
    mark_prior_print: Decimal | None

    def effective_cents(self) -> Decimal | None:
        if self.pre_strict_mid is None:
            return None
        return maker_retention_cents(
            side=self.maker_side, price=self.yes_price, mark=self.pre_strict_mid
        )

    def realised_cents(self, mark: Decimal | None) -> Decimal | None:
        if mark is None:
            return None
        return maker_retention_cents(side=self.maker_side, price=self.yes_price, mark=mark)


def replay_marks(prints: Sequence[TapePrint], *, horizon: timedelta) -> tuple[MarkedFill, ...]:
    """Replay one ticker. Pre-trade anchors exclude the current print."""
    ordered = sorted(prints, key=lambda p: (require_utc(p.ts), p.trade_id))
    bid: Decimal | None = None
    ask: Decimal | None = None
    last_print: Decimal | None = None
    pending: list[tuple[TapePrint, Decimal | None, Decimal | None, MakerSide]] = []
    timeline: list[tuple[datetime, Decimal | None, Decimal | None]] = []

    for print_ in ordered:
        ts = require_utc(print_.ts)
        pre_strict = strict_uncrossed_mid(bid, ask)
        pre_prior = last_print
        side: MakerSide = "sell" if print_.direction == 1 else "buy"
        if print_.direction == 1:
            ask = print_.yes_price
        else:
            bid = print_.yes_price
        last_print = print_.yes_price
        pending.append((print_, pre_strict, pre_prior, side))
        timeline.append((ts, strict_uncrossed_mid(bid, ask), last_print))

    out: list[MarkedFill] = []
    for print_, pre_strict, pre_prior, side in pending:
        target = require_utc(print_.ts) + horizon
        mark_strict: Decimal | None = None
        mark_prior: Decimal | None = None
        for ts, strict, prior in timeline:
            if ts <= target:
                mark_strict = strict
                mark_prior = prior
            else:
                break
        out.append(
            MarkedFill(
                trade_id=print_.trade_id,
                ts=require_utc(print_.ts),
                yes_price=print_.yes_price,
                count=print_.count,
                maker_side=side,
                pre_strict_mid=pre_strict,
                pre_prior_print=pre_prior,
                mark_strict_mid=mark_strict,
                mark_prior_print=mark_prior,
            )
        )
    return tuple(out)


def mm_era_of(climate_day: date) -> Literal["pre_mm_program", "mm_program"]:
    if climate_day >= MM_PROGRAM_START:
        return "mm_program"
    return "pre_mm_program"


@dataclass(frozen=True, slots=True)
class WeightedEstimate:
    point: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    n_days: int


def _percentile_ci(samples: list[float], *, ci: float = 0.95) -> tuple[float, float] | None:
    if len(samples) < 2:
        return None
    ordered = sorted(samples)
    n = len(ordered)
    alpha = (1.0 - ci) / 2.0
    lo = ordered[int(math.floor(alpha * n))]
    hi = ordered[min(n - 1, int(math.ceil((1.0 - alpha) * n)) - 1)]
    return lo, hi


def both_weightings(
    by_day: dict[date, list[float]],
    *,
    seed: int,
    n_resample: int = 1000,
) -> dict[str, WeightedEstimate]:
    """Trade-pooled mean and equal-weight-per-day mean, each with a day-clustered CI."""
    days = [day for day, vals in by_day.items() if vals]
    if not days:
        empty = WeightedEstimate(None, None, None, 0, 0)
        return {"trade_weighted": empty, "day_weighted": empty}

    sums = [sum(by_day[day]) for day in days]
    ns = [len(by_day[day]) for day in days]
    day_means = [sums[i] / ns[i] for i in range(len(days))]
    n_trades = sum(ns)
    trade_point = sum(sums) / n_trades
    day_point = sum(day_means) / len(day_means)

    rng = random.Random(seed)
    trade_draws: list[float] = []
    day_draws: list[float] = []
    n_days = len(days)
    for _ in range(n_resample):
        drawn = [rng.randrange(n_days) for _ in range(n_days)]
        s = 0.0
        c = 0
        d = 0.0
        for idx in drawn:
            s += sums[idx]
            c += ns[idx]
            d += day_means[idx]
        trade_draws.append(s / c)
        day_draws.append(d / n_days)
    trade_ci = _percentile_ci(trade_draws)
    day_ci = _percentile_ci(day_draws)
    return {
        "trade_weighted": WeightedEstimate(
            trade_point,
            trade_ci[0] if trade_ci else None,
            trade_ci[1] if trade_ci else None,
            n_trades,
            n_days,
        ),
        "day_weighted": WeightedEstimate(
            day_point,
            day_ci[0] if day_ci else None,
            day_ci[1] if day_ci else None,
            n_trades,
            n_days,
        ),
    }


def assign_volume_deciles(premium_by_day: dict[date, float]) -> dict[date, int]:
    """Decile 1 is the quietest tenth of climate days; 10 is the busiest."""
    ordered = sorted(premium_by_day, key=lambda day: (premium_by_day[day], day.isoformat()))
    n = len(ordered)
    out: dict[date, int] = {}
    if n == 0:
        return out
    for index, day in enumerate(ordered):
        decile = min(10, int(index * 10 / n) + 1)
        out[day] = decile
    return out


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = 0.0
    dx = 0.0
    dy = 0.0
    for x, y in zip(xs, ys, strict=True):
        vx = x - mx
        vy = y - my
        num += vx * vy
        dx += vx * vx
        dy += vy * vy
    if dx <= 0.0 or dy <= 0.0:
        return None
    return num / math.sqrt(dx * dy)


def correlation_with_ci(
    volume: Sequence[float],
    retention: Sequence[float],
    *,
    seed: int,
    n_resample: int = 1000,
) -> dict[str, float | None]:
    """Pearson correlation of day-level pairs. Interval resamples days."""
    point = pearson(volume, retention)
    n = len(volume)
    if point is None or n < 3:
        return {"point": point, "ci_low": None, "ci_high": None, "n_days": float(n)}
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_resample):
        idx = [rng.randrange(n) for _ in range(n)]
        xs = [volume[i] for i in idx]
        ys = [retention[i] for i in idx]
        value = pearson(xs, ys)
        if value is not None:
            draws.append(value)
    ci = _percentile_ci(draws)
    return {
        "point": point,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
        "n_days": float(n),
    }


def gate0_share(retention_cents: float | None) -> dict[str, float | str | None]:
    """Share of maker volume ``s`` such that ``e * s = 0.0016`` dollars.

    ``e`` is the realised half-spread in dollars per contract (cents / 100).
    No percentage conversion.
    """
    if retention_cents is None:
        return {"status": "NOT_RUN", "reason": "no retention", "e_dollars": None, "s": None}
    e = retention_cents / 100.0
    if e <= 0.0:
        return {
            "status": "impossible",
            "e_dollars": e,
            "s": None,
            "required_e_times_s": GATE0_REQUIRED_E_TIMES_S,
        }
    return {
        "status": "ok",
        "e_dollars": e,
        "s": GATE0_REQUIRED_E_TIMES_S / e,
        "required_e_times_s": GATE0_REQUIRED_E_TIMES_S,
    }


def _estimate_dict(est: WeightedEstimate) -> dict[str, float | int | str | None]:
    return {
        "point_cents_per_contract": est.point,
        "ci_low_cents_per_contract": est.ci_low,
        "ci_high_cents_per_contract": est.ci_high,
        "n_fills": est.n,
        "n_days": est.n_days,
        "unit": "cents_per_contract",
    }


def _difference_ci(
    left: dict[date, list[float]],
    right: dict[date, list[float]],
    *,
    seed: int,
    n_resample: int,
    day_weighted: bool,
) -> dict[str, float | None]:
    """left minus right. Days in each group are resampled independently."""
    left_w = both_weightings(left, seed=seed, n_resample=n_resample)
    right_w = both_weightings(right, seed=seed + 1, n_resample=n_resample)
    key = "day_weighted" if day_weighted else "trade_weighted"
    lp = left_w[key].point
    rp = right_w[key].point
    if lp is None or rp is None:
        return {"point_cents_per_contract": None, "ci_low": None, "ci_high": None}

    def _stats(groups: dict[date, list[float]]) -> tuple[list[float], list[int], list[float]]:
        days = [day for day, vals in groups.items() if vals]
        sums = [sum(groups[day]) for day in days]
        ns = [len(groups[day]) for day in days]
        means = [sums[i] / ns[i] for i in range(len(days))]
        return sums, ns, means

    l_sums, l_ns, l_means = _stats(left)
    r_sums, r_ns, r_means = _stats(right)
    rng = random.Random(seed + (2 if day_weighted else 3))
    draws: list[float] = []
    n_l = len(l_means)
    n_r = len(r_means)
    if n_l < 2 or n_r < 2:
        return {
            "point_cents_per_contract": lp - rp,
            "ci_low": None,
            "ci_high": None,
        }
    for _ in range(n_resample):
        if day_weighted:
            dl = sum(l_means[rng.randrange(n_l)] for _ in range(n_l)) / n_l
            dr = sum(r_means[rng.randrange(n_r)] for _ in range(n_r)) / n_r
        else:
            ls = 0.0
            lc = 0
            rs = 0.0
            rc = 0
            for _ in range(n_l):
                idx = rng.randrange(n_l)
                ls += l_sums[idx]
                lc += l_ns[idx]
            for _ in range(n_r):
                idx = rng.randrange(n_r)
                rs += r_sums[idx]
                rc += r_ns[idx]
            dl = ls / lc
            dr = rs / rc
        draws.append(dl - dr)
    ci = _percentile_ci(draws)
    return {
        "point_cents_per_contract": lp - rp,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
    }


@dataclass(frozen=True, slots=True)
class SettlementFill:
    climate_day: date
    season: str
    mm_era: str
    retention_cents: float
    effective_cents: float | None
    premium: float


def summarise_retention(
    fills: Sequence[SettlementFill],
    *,
    seed: int = 0,
    n_resample: int = 1000,
) -> dict[str, object]:
    """Primary C1-M2 table. Every headline carries both weightings, in cents."""
    by_day: dict[date, list[float]] = defaultdict(list)
    premium_by_day: dict[date, float] = defaultdict(float)
    for fill in fills:
        by_day[fill.climate_day].append(fill.retention_cents)
        premium_by_day[fill.climate_day] += fill.premium

    decile_of = assign_volume_deciles(dict(premium_by_day))
    decile_groups: dict[int, dict[date, list[float]]] = {k: defaultdict(list) for k in range(1, 11)}
    for fill in fills:
        decile_groups[decile_of[fill.climate_day]][fill.climate_day].append(fill.retention_cents)

    day_volume: list[float] = []
    day_retention: list[float] = []
    for day, vals in by_day.items():
        day_volume.append(premium_by_day[day])
        day_retention.append(sum(vals) / len(vals))

    top = decile_groups[10]
    bottom: dict[date, list[float]] = defaultdict(list)
    for decile in range(1, 6):
        for day, vals in decile_groups[decile].items():
            bottom[day].extend(vals)

    def _slice(pred: Callable[[SettlementFill], bool]) -> dict[date, list[float]]:
        grouped: dict[date, list[float]] = defaultdict(list)
        for fill in fills:
            if pred(fill):
                grouped[fill.climate_day].append(fill.retention_cents)
        return grouped

    def _is_jja(fill: SettlementFill) -> bool:
        return fill.season == "JJA"

    jja_groups = _slice(_is_jja)
    era_groups: dict[str, dict[date, list[float]]] = {
        "pre_mm_program": defaultdict(list),
        "mm_program": defaultdict(list),
    }
    for fill in fills:
        era_groups[fill.mm_era][fill.climate_day].append(fill.retention_cents)

    def _pair(
        groups: dict[date, list[float]], pair_seed: int
    ) -> dict[str, dict[str, float | int | str | None]]:
        weighted = both_weightings(groups, seed=pair_seed, n_resample=n_resample)
        return {
            "trade_weighted": _estimate_dict(weighted["trade_weighted"]),
            "day_weighted": _estimate_dict(weighted["day_weighted"]),
        }

    headline = both_weightings(dict(by_day), seed=seed, n_resample=n_resample)
    return {
        "unit": "cents_per_contract",
        "both_weightings_required": True,
        "headline_settlement_retention": {
            "trade_weighted": _estimate_dict(headline["trade_weighted"]),
            "day_weighted": _estimate_dict(headline["day_weighted"]),
        },
        "by_volume_decile": {
            str(decile): {
                **_pair(dict(decile_groups[decile]), seed + decile),
                "n_days": len(decile_groups[decile]),
            }
            for decile in range(1, 11)
        },
        "volume_retention_correlation": correlation_with_ci(
            day_volume, day_retention, seed=seed + 20, n_resample=n_resample
        ),
        "top_decile_minus_bottom_five": {
            "trade_weighted": _difference_ci(
                dict(top), dict(bottom), seed=seed + 30, n_resample=n_resample, day_weighted=False
            ),
            "day_weighted": _difference_ci(
                dict(top), dict(bottom), seed=seed + 30, n_resample=n_resample, day_weighted=True
            ),
        },
        "jja": _pair(jja_groups, seed + 40),
        "by_mm_era": {
            era: _pair(groups, seed + 50)
            for era, groups in era_groups.items()
        },
        "gate0": {
            "note": (
                "e is realised half-spread in dollars per contract. "
                "s = 0.0016 / e when e > 0. Both weightings. Not a percentage."
            ),
            "trade_weighted": _gate_triple(headline["trade_weighted"]),
            "day_weighted": _gate_triple(headline["day_weighted"]),
        },
    }


def _gate_triple(est: WeightedEstimate) -> dict[str, object]:
    return {
        "point": gate0_share(est.point),
        "ci_low": gate0_share(est.ci_low),
        "ci_high": gate0_share(est.ci_high),
    }
