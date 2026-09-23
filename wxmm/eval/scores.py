"""RPS, log score, and Brier decomposition.

Allowed to assume
    Bracket partitions are exhaustive for the scored day.

Must never
    Compare ``RES`` across models with different binning.
    Score the point outcome indicator. Infer bracket order from ticker strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import log
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class BrierComponents:
    brier: Decimal
    reliability: Decimal
    resolution: Decimal
    uncertainty: Decimal

    def reconcile(self, *, tol: float = 0.01) -> bool:
        """``BS ≈ REL − RES + UNC`` within tolerance."""
        lhs = float(self.brier)
        rhs = float(self.reliability - self.resolution + self.uncertainty)
        return abs(lhs - rhs) <= tol * max(abs(lhs), abs(rhs), 1e-9)


def ranked_probability_score(
    forecast: Mapping[str, Decimal],
    realised_market_id: str,
    *,
    order: Sequence[str],
) -> Decimal:
    """RPS over an ordered bracket partition.

    ``order`` is required and must list exactly the forecast brackets.
    The score is ``sum_k (F_k - O_k)^2`` with ``F`` the forecast CDF and
    ``O_k = 1{y <= k}`` the cumulative outcome indicator.
    """
    if len(order) != len(set(order)) or set(order) != set(forecast):
        raise ValueError("order must list exactly the forecast brackets")
    if realised_market_id not in forecast:
        raise ValueError("realised bracket missing from forecast")
    cumulative = Decimal(0)
    outcome = Decimal(0)
    score = Decimal(0)
    for key in order:
        p = forecast[key]
        if key == realised_market_id:
            outcome = Decimal(1)
        score += (cumulative + p - outcome) ** 2
        cumulative += p
    return score


def log_score(forecast: Mapping[str, Decimal], realised_market_id: str) -> Decimal:
    p = forecast.get(realised_market_id)
    if p is None or p <= Decimal(0):
        raise ValueError("log score undefined for missing or zero forecast mass")
    return Decimal(str(-log(float(p))))


def brier_decomposition(
    forecasts: Sequence[Mapping[str, Decimal]],
    outcomes: Sequence[str],
    *,
    n_bins: int = 10,
) -> BrierComponents:
    """Per-bracket Brier decomposition aggregated across days/contracts."""
    if len(forecasts) != len(outcomes) or not forecasts:
        raise ValueError("forecasts and outcomes must be same non-empty length")
    # Flatten (market_id, p, y) tuples
    rows: list[tuple[Decimal, int]] = []
    for forecast, outcome in zip(forecasts, outcomes, strict=True):
        for market_id, p in forecast.items():
            y = 1 if market_id == outcome else 0
            rows.append((p, y))
    if not rows:
        raise ValueError("empty decomposition input")
    probs = [float(p) for p, _y in rows]
    ys = [y for _p, y in rows]
    n = len(rows)
    bs = sum((float(p) - y) ** 2 for p, y in rows) / n
    y_bar = sum(ys) / n
    unc = y_bar * (1.0 - y_bar)
    # Reliability / resolution via equal-width bins on predicted p
    bin_edges = [i / n_bins for i in range(n_bins + 1)]
    rel = 0.0
    res = 0.0
    for b in range(n_bins):
        lo, hi = bin_edges[b], bin_edges[b + 1]
        last = b == n_bins - 1
        idx = [i for i, p in enumerate(probs) if p >= lo and (p < hi or (last and p <= hi))]
        if not idx:
            continue
        n_k = len(idx)
        o_k = sum(ys[i] for i in idx) / n_k
        p_k = sum(probs[i] for i in idx) / n_k
        rel += (n_k / n) * (p_k - o_k) ** 2
        res += (n_k / n) * (o_k - y_bar) ** 2
    return BrierComponents(
        brier=Decimal(str(bs)),
        reliability=Decimal(str(rel)),
        resolution=Decimal(str(res)),
        uncertainty=Decimal(str(unc)),
    )
