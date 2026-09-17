"""Mark-out after real fills. Distribution plus fill count, never a lone mean.

Allowed to assume
    Horizons are +1m, +5m, +30m, and settlement, against venue mid.

Must never
    Report only a mean. Hide the fill count. Invent a mid when the book
    is empty (skip that horizon).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from wxmm.core.utc import require_utc

HORIZONS: tuple[tuple[str, timedelta | None], ...] = (
    ("1m", timedelta(minutes=1)),
    ("5m", timedelta(minutes=5)),
    ("30m", timedelta(minutes=30)),
    ("settlement", None),
)


@dataclass(frozen=True, slots=True)
class FillMark:
    fill_id: str
    fill_ts: datetime
    fill_price_cents: int
    side: str
    mid_at_horizon_cents: int | None
    horizon: str
    markout_cents: int | None


@dataclass(frozen=True, slots=True)
class MarkoutDistribution:
    horizon: str
    fill_count: int
    markouts_cents: tuple[int, ...]
    n_missing_mid: int

    def mean_cents(self) -> Decimal | None:
        if not self.markouts_cents:
            return None
        return Decimal(sum(self.markouts_cents)) / Decimal(len(self.markouts_cents))


def markout_cents(*, side: str, fill_price_cents: int, mid_cents: int) -> int:
    """Signed vs mid: buy wants mid to rise; sell wants mid to fall."""
    if side == "buy":
        return mid_cents - fill_price_cents
    if side == "sell":
        return fill_price_cents - mid_cents
    raise ValueError(f"side must be buy|sell, not {side!r}")


def marks_for_fill(
    *,
    fill_id: str,
    fill_ts: datetime,
    fill_price_cents: int,
    side: str,
    mids: dict[datetime, int],
    settlement_mid_cents: int | None,
) -> tuple[FillMark, ...]:
    fill_ts = require_utc(fill_ts)
    out: list[FillMark] = []
    for name, delta in HORIZONS:
        if delta is None:
            mid = settlement_mid_cents
        else:
            target = fill_ts + delta
            mid = _mid_at_or_after(mids, target)
        mo = None if mid is None else markout_cents(
            side=side, fill_price_cents=fill_price_cents, mid_cents=mid
        )
        out.append(
            FillMark(
                fill_id=fill_id,
                fill_ts=fill_ts,
                fill_price_cents=fill_price_cents,
                side=side,
                mid_at_horizon_cents=mid,
                horizon=name,
                markout_cents=mo,
            )
        )
    return tuple(out)


def distribution(marks: tuple[FillMark, ...] | list[FillMark], horizon: str) -> MarkoutDistribution:
    rows = [m for m in marks if m.horizon == horizon]
    values = tuple(m.markout_cents for m in rows if m.markout_cents is not None)
    missing = sum(1 for m in rows if m.markout_cents is None)
    return MarkoutDistribution(
        horizon=horizon,
        fill_count=len(rows),
        markouts_cents=values,
        n_missing_mid=missing,
    )


def _mid_at_or_after(mids: dict[datetime, int], target: datetime) -> int | None:
    eligible = [(ts, px) for ts, px in mids.items() if require_utc(ts) >= target]
    if not eligible:
        return None
    ts, px = min(eligible, key=lambda row: row[0])
    _ = ts
    return px
