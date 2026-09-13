"""v0 baselines that are not the null.

Under the anchored architecture β = 0 *is* the normalised trade-derived
market ladder. There is no separate market baseline.

Remaining baselines:
    climatology — bracket-position frequencies on the same LST doy window
    persistence — raw last-trade yes_price, uncorrected for direction
    nbm_ladder — UNAVAILABLE_INTERPOLATION_UNSPECIFIED (do not omit)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from wxmm.analysis.maker_taker import SettlementLabel
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.core.errors import NwpInterpolationUnspecified
from wxmm.fairvalue.anchor import apply_logit_adjustment
from wxmm.fairvalue.ladder import parse_kalshi_bracket

NBM_STATUS: Literal["UNAVAILABLE_INTERPOLATION_UNSPECIFIED"] = (
    "UNAVAILABLE_INTERPOLATION_UNSPECIFIED"
)
DOY_WINDOW_DEFAULT = 15


def _position_key(ticker: str) -> tuple[int, str]:
    parsed = parse_kalshi_bracket(ticker)
    if parsed is None:
        return (10**9, ticker)
    floor = parsed.floor_f if parsed.floor_f is not None else -10**9
    cap = parsed.cap_f if parsed.cap_f is not None else 10**9
    return (floor, f"{cap}:{ticker}")


def sort_ladder(tickers: Sequence[str]) -> list[str]:
    return sorted(tickers, key=_position_key)


def _doy_distance(left: int, right: int) -> int:
    raw = abs(left - right)
    return min(raw, 366 - raw)


def climatology_forecast(
    labels: Mapping[str, SettlementLabel],
    tickers: Sequence[str],
    *,
    predict_day: date,
    train_days: Sequence[date],
    window_days: int = DOY_WINDOW_DEFAULT,
) -> dict[str, Decimal] | None:
    """P(position i wins) on the same LST doy window. Training days only."""
    ordered = sort_ladder(tickers)
    if not ordered:
        return None
    train = set(train_days)
    target_doy = predict_day.timetuple().tm_yday
    wins = [0] * len(ordered)
    n = 0
    by_day: dict[date, list[SettlementLabel]] = {}
    for label in labels.values():
        if label.climate_day not in train:
            continue
        if label.source != "clinyc_as_issued":
            continue
        if _doy_distance(label.climate_day.timetuple().tm_yday, target_doy) > window_days:
            continue
        by_day.setdefault(label.climate_day, []).append(label)
    for day_labels in by_day.values():
        day_tickers = sort_ladder([row.ticker for row in day_labels])
        if len(day_tickers) != len(ordered):
            continue
        won = [row for row in day_labels if row.yes_won]
        if len(won) != 1:
            continue
        # Map by sorted position, not ticker string (thresholds move).
        try:
            position = day_tickers.index(won[0].ticker)
        except ValueError:
            continue
        wins[position] += 1
        n += 1
    if n == 0:
        return None
    return {ticker: Decimal(wins[i]) / Decimal(n) for i, ticker in enumerate(ordered)}


def persistence_forecast(
    trades: Sequence[RawTrade],
    tickers: Sequence[str],
) -> dict[str, Decimal] | None:
    """Raw last-trade yes_price, no direction correction, then renormalise."""
    last: dict[str, RawTrade] = {}
    for trade in trades:
        if trade.is_block_trade or trade.ticker not in tickers:
            continue
        prev = last.get(trade.ticker)
        if prev is None or trade.created_time >= prev.created_time:
            last[trade.ticker] = trade
    if any(ticker not in last for ticker in tickers):
        return None
    raw = {ticker: last[ticker].yes_price for ticker in tickers}
    total = sum(raw.values(), Decimal("0"))
    if total <= 0:
        return None
    return {ticker: price / total for ticker, price in raw.items()}


def nbm_forecast(*_args: object, **_kwargs: object) -> None:
    raise NwpInterpolationUnspecified(
        f"nbm_ladder {NBM_STATUS}; K2 interpolation method is unspecified"
    )


def null_equals_market(q_normalised: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Identity: β = 0 recovers q. Exists so nobody adds a second market baseline."""
    zero = {key: Decimal("0") for key in q_normalised}
    return apply_logit_adjustment(q_normalised, zero)

