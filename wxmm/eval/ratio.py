"""Pairwise bracket-ratio calibration.

Allowed to assume
    Marginal reliability can look fine while relative ordering is wrong.

Must never
    Report a single ratio without fill/event count.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RatioBin:
    pair: tuple[str, str]
    predicted_ratio: Decimal
    realised_ratio: Decimal | None
    n_events: int


def pairwise_ratio_table(
    forecasts: Sequence[Mapping[str, Decimal]],
    outcomes: Sequence[str],
) -> tuple[RatioBin, ...]:
    """For each unordered pair, compare predicted and realised i/j frequencies."""
    if len(forecasts) != len(outcomes):
        raise ValueError("forecasts and outcomes length mismatch")
    pairs: dict[tuple[str, str], list[tuple[Decimal, int | None]]] = {}
    market_ids = sorted(forecasts[0].keys()) if forecasts else []
    for i, mid_i in enumerate(market_ids):
        for mid_j in market_ids[i + 1 :]:
            pairs[(mid_i, mid_j)] = []
    for forecast, outcome in zip(forecasts, outcomes, strict=True):
        for (a, b), bucket in pairs.items():
            pa, pb = forecast[a], forecast[b]
            if pb <= Decimal(0):
                continue
            pred = pa / pb
            realised: int | None
            if outcome == a:
                realised = 1
            elif outcome == b:
                realised = 0
            else:
                realised = None
            bucket.append((pred, realised))
    out: list[RatioBin] = []
    for pair, rows in sorted(pairs.items()):
        if not rows:
            continue
        preds = [r[0] for r in rows]
        hits = [r[1] for r in rows if r[1] is not None]
        realised_ratio = None
        if hits:
            realised_ratio = Decimal(sum(hits)) / Decimal(len(hits))
        out.append(
            RatioBin(
                pair=pair,
                predicted_ratio=sum(preds, Decimal(0)) / Decimal(len(preds)),
                realised_ratio=realised_ratio,
                n_events=len(rows),
            )
        )
    return tuple(out)
