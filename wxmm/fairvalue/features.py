"""Feature blocks for the trade-derived path: flow and calendar only.

The reconstructed ``book`` block is still Stage-0 gated. ``nwp`` waits on the
K2 interpolation write-up. ``obs`` waits on C1-T1.

Must never
    Read a reconstructed book. Impute missing staleness. Drop per-side
    staleness from the vector. Invent a book feature that looks like flow.
    Build an nwp block before the interpolation method is written.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from wxmm.analysis.maker_taker import season_of
from wxmm.analysis.trades_ingest import RawTrade, parse_climate_day
from wxmm.core.errors import NwpInterpolationUnspecified, ReconstructionBoundRequired
from wxmm.core.utc import require_utc
from wxmm.fairvalue.anchor_trades import (
    ObservedMapping,
    TradeImpliedBook,
    YesSpacePrint,
    assert_outcome_bookside_mapping,
    filter_trades_as_of,
    implied_book_from_trades,
    refuse_if_leaked,
    yes_space_print,
)
from wxmm.settlement.eras import kalshi_last_trading_close_utc

DEFAULT_FLOW_WINDOWS: tuple[timedelta, ...] = (
    timedelta(minutes=15),
    timedelta(minutes=60),
    timedelta(minutes=240),
)
STALENESS_KEYS: tuple[str, ...] = (
    "bid_staleness_seconds",
    "ask_staleness_seconds",
    "staleness_ratio",
)
WINDOW_INVARIANT_KEYS: tuple[str, ...] = (
    "implied_spread",
    "bid_staleness_seconds",
    "ask_staleness_seconds",
    "staleness_ratio",
    "crossed_count",
)
"""Read off the as-of book, which the window does not touch.

Emitting these once per window puts identical columns in the design matrix.
Ridge then splits one effect across the copies and penalises the direction at
lambda/k instead of lambda. Consumers must emit them once.
"""
DOY_PERIOD = 365.25


@dataclass(frozen=True, slots=True)
class FlowFeatures:
    window: timedelta
    signed_ofi: Decimal | None
    implied_spread: Decimal | None
    implied_spread_change: Decimal | None
    bid_staleness_seconds: float | None
    ask_staleness_seconds: float | None
    staleness_ratio: float | None
    crossed_count: int
    trade_count: int
    notional: Decimal
    intensity_per_hour: float | None
    gap_since_last_seconds: float | None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "signed_ofi": None if self.signed_ofi is None else float(self.signed_ofi),
            "implied_spread": (
                None if self.implied_spread is None else float(self.implied_spread)
            ),
            "implied_spread_change": (
                None
                if self.implied_spread_change is None
                else float(self.implied_spread_change)
            ),
            "bid_staleness_seconds": self.bid_staleness_seconds,
            "ask_staleness_seconds": self.ask_staleness_seconds,
            "staleness_ratio": self.staleness_ratio,
            "crossed_count": float(self.crossed_count),
            "trade_count": float(self.trade_count),
            "notional": float(self.notional),
            "intensity_per_hour": self.intensity_per_hour,
            "gap_since_last_seconds": self.gap_since_last_seconds,
        }


@dataclass(frozen=True, slots=True)
class CalendarFeatures:
    season: str
    doy: int
    doy_sin: float
    doy_cos: float
    hours_to_close: float | None


def assert_staleness_surfaced(features: Mapping[str, float | None]) -> None:
    """Consumers that drop per-side staleness fail here. Do not 'fix' by omitting."""
    missing = [key for key in STALENESS_KEYS if key not in features]
    if missing:
        raise AssertionError(f"staleness dropped from feature vector: {missing}")


def doy_harmonics(doy: int) -> tuple[float, float]:
    """First Fourier pair on the climate-day day-of-year. Not a linear doy trend."""
    angle = 2.0 * math.pi * float(doy) / DOY_PERIOD
    return math.sin(angle), math.cos(angle)


def book_features(*_args: object, **_kwargs: object) -> None:
    raise ReconstructionBoundRequired(
        "book feature block is Stage 0 gated; use flow and calendar on TRADE_DERIVED"
    )


def nwp_features(*_args: object, **_kwargs: object) -> None:
    raise NwpInterpolationUnspecified(
        "nwp block blocked on K2 decile-to-bracket interpolation "
        f"({NwpInterpolationUnspecified.status}); write the method, then "
        "verify a known Gaussian returns its own bracket masses"
    )


def _prints_in_window(
    trades: Sequence[RawTrade],
    *,
    as_of: datetime,
    window: timedelta,
    ticker: str | None,
) -> list[YesSpacePrint]:
    start = require_utc(as_of) - window
    refuse_if_leaked(trades, as_of, ticker=ticker)
    out: list[YesSpacePrint] = []
    for trade in trades:
        if ticker is not None and trade.ticker != ticker:
            continue
        if trade.is_block_trade:
            continue
        if trade.created_time < start:
            continue
        out.append(yes_space_print(trade))
    return out


def flow_features(
    trades: Sequence[RawTrade],
    *,
    as_of: datetime,
    window: timedelta,
    ticker: str | None = None,
    book: TradeImpliedBook | None = None,
    prior_spread: Decimal | None = None,
    mapping: ObservedMapping | None = None,
) -> FlowFeatures:
    as_of_utc = require_utc(as_of)
    refuse_if_leaked(trades, as_of_utc, ticker=ticker)
    scoped = filter_trades_as_of(trades, as_of_utc, ticker=ticker)
    prints = _prints_in_window(scoped, as_of=as_of_utc, window=window, ticker=ticker)
    signed = Decimal("0")
    gross = Decimal("0")
    notional = Decimal("0")
    for item in prints:
        signed += Decimal(item.direction) * item.count
        gross += item.count
        notional += item.count * item.yes_price
    ofi = (signed / gross) if gross else None
    # Mapping is a corpus property. Do not re-assert on a ticker- or window-filter.
    # Resolving it once here keeps the lagged book below off a narrower slice than
    # the one the caller's cross-tab gate already cleared.
    resolved = mapping
    if resolved is None:
        non_block = [trade for trade in scoped if not trade.is_block_trade]
        if non_block:
            resolved = assert_outcome_bookside_mapping(non_block)
    snapshot = book or implied_book_from_trades(
        scoped, as_of=as_of_utc, ticker=ticker, mapping=resolved
    )
    bid_s = (
        snapshot.bid_staleness.total_seconds() if snapshot.bid_staleness is not None else None
    )
    ask_s = (
        snapshot.ask_staleness.total_seconds() if snapshot.ask_staleness is not None else None
    )
    ratio = None
    if bid_s is not None and ask_s is not None and ask_s > 0:
        ratio = bid_s / ask_s
    hours = window.total_seconds() / 3600.0
    intensity = (len(prints) / hours) if hours > 0 else None
    last = max((p.created_time for p in prints), default=None)
    gap = (as_of_utc - last).total_seconds() if last is not None else None
    spread = snapshot.implied_spread
    if prior_spread is None and spread is not None:
        lagged_as_of = as_of_utc - window
        lagged = implied_book_from_trades(
            filter_trades_as_of(scoped, lagged_as_of, ticker=ticker),
            as_of=lagged_as_of,
            ticker=ticker,
            mapping=resolved,
        )
        prior_spread = lagged.implied_spread
    change = None if spread is None or prior_spread is None else spread - prior_spread
    features = FlowFeatures(
        window=window,
        signed_ofi=ofi,
        implied_spread=spread,
        implied_spread_change=change,
        bid_staleness_seconds=bid_s,
        ask_staleness_seconds=ask_s,
        staleness_ratio=ratio,
        crossed_count=snapshot.crossed_invalidations,
        trade_count=len(prints),
        notional=notional,
        intensity_per_hour=intensity,
        gap_since_last_seconds=gap,
    )
    assert_staleness_surfaced(features.as_dict())
    return features


def flow_features_multiwindow(
    trades: Sequence[RawTrade],
    *,
    as_of: datetime,
    ticker: str | None = None,
    windows: Sequence[timedelta] = DEFAULT_FLOW_WINDOWS,
    mapping: ObservedMapping | None = None,
) -> dict[str, FlowFeatures]:
    if len(windows) < 3:
        raise ValueError("skill must be reported across at least three flow windows")
    return {
        f"w{int(window.total_seconds())}s": flow_features(
            trades, as_of=as_of, window=window, ticker=ticker, mapping=mapping
        )
        for window in windows
    }


def calendar_features(ticker: str, as_of: datetime) -> CalendarFeatures:
    climate = parse_climate_day(ticker)
    close = kalshi_last_trading_close_utc(climate)
    now = require_utc(as_of)
    hours = (close - now).total_seconds() / 3600.0
    doy = climate.timetuple().tm_yday
    sine, cosine = doy_harmonics(doy)
    return CalendarFeatures(
        season=season_of(climate),
        doy=doy,
        doy_sin=sine,
        doy_cos=cosine,
        hours_to_close=hours,
    )
