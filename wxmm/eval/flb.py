"""Favorite-longshot regression on KXHIGHNY (X1c).

Bürgi, Deng and Whelan (CESifo WP 12122) eq. (4), Climate & Weather:

    y − p = α + ψ p + ε

with p, y in percent (0–100) so the published Climate & Weather numbers
are comparable: ψ = 0.031 (SE 0.005), α = −0.997 (SE 0.243), n = 29,924.

Then maker return by price decile, and specifically the return to
resting offers below 10¢ — where C1-T1's exclusion trade sits and the
taker fee is 0.333¢/contract rather than 1.750¢.

Must never
    Read a book or candle. Report these as strategy P&L.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from wxmm.analysis.maker_taker import AttributedTrade, CompactFill, ReturnKind, _mean

PUBLISHED_CLIMATE_WEATHER_PSI = Decimal("0.031")
PUBLISHED_CLIMATE_WEATHER_PSI_SE = Decimal("0.005")
PUBLISHED_CLIMATE_WEATHER_ALPHA = Decimal("-0.997")
PUBLISHED_CLIMATE_WEATHER_ALPHA_SE = Decimal("0.243")
PUBLISHED_CLIMATE_WEATHER_N = 29924
PUBLISHED_CLIMATE_WEATHER = {
    "psi": PUBLISHED_CLIMATE_WEATHER_PSI,
    "psi_se": PUBLISHED_CLIMATE_WEATHER_PSI_SE,
    "alpha": PUBLISHED_CLIMATE_WEATHER_ALPHA,
    "alpha_se": PUBLISHED_CLIMATE_WEATHER_ALPHA_SE,
    "n": PUBLISHED_CLIMATE_WEATHER_N,
}

# 1-contract 10¢ taker fee under per_order_cent: round_up(0.07*1*0.10*0.90)=0.0063→$0.01
# At C=1 the documented 0.333¢ is the continuous 0.07*0.10*0.90=0.0063? Wait:
# 0.07 * 1 * 0.10 * 0.90 = 0.0063 dollars = 0.63¢, not 0.333¢.
# Prompt states 0.333¢ at the exclusion wing vs 1.750¢ mid-band.
# 0.07 * 0.05 * 0.95 ≈ 0.003325 → 0.333¢ at ~5¢. 1.750¢ is 0.07*0.50*0.50.
# Record the mid-band vs wing fee as named constants; do not re-derive mid-band.
WING_TAKER_FEE_CENTS_AT_5C = Decimal("0.333")
MID_TAKER_FEE_CENTS_AT_50C = Decimal("1.750")


class FlbEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alpha: float
    alpha_se: float
    psi: float
    psi_se: float
    n: int
    n_clusters: int
    specification: str = "y_pct - p_pct = alpha + psi * p_pct  (Bur26 eq. 4, percent units)"


class DecileRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decile: int
    p_lo: Decimal
    p_hi: Decimal
    n_trades: int
    maker_mean_net_return: Decimal
    maker_mean_gross_return: Decimal


class X1cReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReturnKind = "historical_resting_counterparty_return"
    is_strategy_pnl: Literal[False] = False
    estimate: FlbEstimate
    published_psi: Decimal = PUBLISHED_CLIMATE_WEATHER_PSI
    published_psi_se: Decimal = PUBLISHED_CLIMATE_WEATHER_PSI_SE
    published_alpha: Decimal = PUBLISHED_CLIMATE_WEATHER_ALPHA
    published_alpha_se: Decimal = PUBLISHED_CLIMATE_WEATHER_ALPHA_SE
    published_n: int = PUBLISHED_CLIMATE_WEATHER_N
    deciles: tuple[DecileRow, ...]
    resting_offer_below_10c_n: int
    resting_offer_below_10c_maker_mean_net: Decimal | None
    resting_offer_below_10c_maker_mean_gross: Decimal | None
    wing_vs_mid_taker_fee_cents: tuple[Decimal, Decimal] = (
        WING_TAKER_FEE_CENTS_AT_5C,
        MID_TAKER_FEE_CENTS_AT_50C,
    )


def _ols_clustered(
    y: list[float],
    x: list[float],
    clusters: list[str],
) -> FlbEstimate:
    """2-parameter OLS with climate-day clustered SE. No pandas."""
    n = len(y)
    if n < 3:
        raise ValueError("FLB regression needs at least 3 observations")
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(a * a for a in x)
    sum_xy = sum(a * b for a, b in zip(x, y, strict=True))
    det = n * sum_xx - sum_x * sum_x
    if det == 0:
        raise ValueError("FLB design matrix is singular")
    psi = (n * sum_xy - sum_x * sum_y) / det
    alpha = (sum_y - psi * sum_x) / n
    resid = [yi - alpha - psi * xi for yi, xi in zip(y, x, strict=True)]

    meat_aa = 0.0
    meat_ap = 0.0
    meat_pp = 0.0
    grouped: dict[str, list[int]] = {}
    for i, key in enumerate(clusters):
        grouped.setdefault(key, []).append(i)
    g = len(grouped)
    for idxs in grouped.values():
        sa = 0.0
        sp = 0.0
        for i in idxs:
            sa += resid[i]
            sp += x[i] * resid[i]
        meat_aa += sa * sa
        meat_ap += sa * sp
        meat_pp += sp * sp
    # (X'X)^{-1} = 1/det * [[sum_xx, -sum_x], [-sum_x, n]]
    inv00 = sum_xx / det
    inv01 = -sum_x / det
    inv11 = n / det
    # V = (X'X)^{-1} meat (X'X)^{-1}
    # meat = [[meat_aa, meat_ap], [meat_ap, meat_pp]]
    t00 = inv00 * meat_aa + inv01 * meat_ap
    t01 = inv00 * meat_ap + inv01 * meat_pp
    t10 = inv01 * meat_aa + inv11 * meat_ap
    t11 = inv01 * meat_ap + inv11 * meat_pp
    v_alpha = t00 * inv00 + t01 * inv01
    v_psi = t10 * inv01 + t11 * inv11
    dfc = (g / (g - 1)) * ((n - 1) / (n - 2)) if g > 1 else 1.0
    alpha_se = (max(v_alpha, 0.0) * dfc) ** 0.5
    psi_se = (max(v_psi, 0.0) * dfc) ** 0.5
    return FlbEstimate(
        alpha=alpha,
        alpha_se=alpha_se,
        psi=psi,
        psi_se=psi_se,
        n=n,
        n_clusters=g,
    )


def _taker_yes_price(row: AttributedTrade) -> Decimal:
    return row.taker.price if row.taker.outcome_side == "yes" else row.yes_price


def favorite_longshot(rows: Sequence[AttributedTrade]) -> FlbEstimate:
    """One row per trade, YES contract: y_yes − p_yes on percent scale."""
    y: list[float] = []
    x: list[float] = []
    clusters: list[str] = []
    for row in rows:
        p = float(row.yes_price) * 100.0
        won = 100.0 if row.yes_won else 0.0
        y.append(won - p)
        x.append(p)
        clusters.append(row.climate_day.isoformat())
    return _ols_clustered(y, x, clusters)


def _deciles(rows: Sequence[AttributedTrade]) -> tuple[DecileRow, ...]:
    if not rows:
        return ()
    ordered = sorted(rows, key=lambda r: r.maker.price)
    n = len(ordered)
    out: list[DecileRow] = []
    for d in range(10):
        start = (d * n) // 10
        end = ((d + 1) * n) // 10
        chunk = ordered[start:end]
        if not chunk:
            continue
        prices = [row.maker.price for row in chunk]
        out.append(
            DecileRow(
                decile=d + 1,
                p_lo=min(prices),
                p_hi=max(prices),
                n_trades=len(chunk),
                maker_mean_net_return=_mean([row.maker.net_return for row in chunk]),
                maker_mean_gross_return=_mean([row.maker.gross_return for row in chunk]),
            )
        )
    return tuple(out)


def resting_offers_below_10c(rows: Sequence[AttributedTrade]) -> list[AttributedTrade]:
    """Maker sold (taker lifted the ask) at a purchased-by-taker price < 10¢."""
    return [
        row
        for row in rows
        if row.taker_book_side == "ask" and row.taker.price < Decimal("0.10")
    ]


def x1c_report(rows: Sequence[AttributedTrade]) -> X1cReport:
    wings = resting_offers_below_10c(rows)
    return X1cReport(
        estimate=favorite_longshot(rows) if len(rows) >= 3 else FlbEstimate(
            alpha=0.0,
            alpha_se=0.0,
            psi=0.0,
            psi_se=0.0,
            n=len(rows),
            n_clusters=0,
        ),
        deciles=_deciles(rows),
        resting_offer_below_10c_n=len(wings),
        resting_offer_below_10c_maker_mean_net=(
            _mean([row.maker.net_return for row in wings]) if wings else None
        ),
        resting_offer_below_10c_maker_mean_gross=(
            _mean([row.maker.gross_return for row in wings]) if wings else None
        ),
    )


def x1c_report_compact(rows: Sequence[CompactFill]) -> X1cReport:
    y: list[float] = []
    x: list[float] = []
    clusters: list[str] = []
    for row in rows:
        p = row.yes_price * 100.0
        won = 100.0 if row.yes_won else 0.0
        y.append(won - p)
        x.append(p)
        clusters.append(row.climate_day.isoformat())
    estimate = (
        _ols_clustered(y, x, clusters)
        if len(rows) >= 3
        else FlbEstimate(
            alpha=0.0,
            alpha_se=0.0,
            psi=0.0,
            psi_se=0.0,
            n=len(rows),
            n_clusters=0,
        )
    )
    ordered = sorted(rows, key=lambda r: r.maker_price)
    n = len(ordered)
    deciles: list[DecileRow] = []
    if n:
        for d in range(10):
            start = (d * n) // 10
            end = ((d + 1) * n) // 10
            chunk = ordered[start:end]
            if not chunk:
                continue
            prices = [Decimal(f"{row.maker_price:.4f}") for row in chunk]
            deciles.append(
                DecileRow(
                    decile=d + 1,
                    p_lo=min(prices),
                    p_hi=max(prices),
                    n_trades=len(chunk),
                    maker_mean_net_return=_mean(
                        [Decimal(str(row.maker_net)) for row in chunk]
                    ),
                    maker_mean_gross_return=_mean(
                        [Decimal(str(row.maker_gross)) for row in chunk]
                    ),
                )
            )
    wings = [
        row
        for row in rows
        if row.taker_book_side == "ask" and row.taker_price < 0.10
    ]
    return X1cReport(
        estimate=estimate,
        deciles=tuple(deciles),
        resting_offer_below_10c_n=len(wings),
        resting_offer_below_10c_maker_mean_net=(
            _mean([Decimal(str(row.maker_net)) for row in wings]) if wings else None
        ),
        resting_offer_below_10c_maker_mean_gross=(
            _mean([Decimal(str(row.maker_gross)) for row in wings]) if wings else None
        ),
    )
