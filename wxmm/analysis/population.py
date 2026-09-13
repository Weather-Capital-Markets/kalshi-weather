"""X1b: is the maker population two-sided or directional?

Per bracket-day, aggregate maker-side fills into a net position
(contracts bought minus contracts sold) and net premium.

If makers finish net flat, the population is on net two-sided and X1a's
maker number is spread minus adverse selection — our estimand.

If makers finish net long or net short, the population is directional,
X1a's maker number is the return on a directional bet, and it says
little about two-sided quoting.

X1a without X1b is uninterpretable: it cannot tell a losing market maker
from a losing directional trader, and those point opposite ways for this
project.

Nobody has published this split. Report the distribution of net maker
position across bracket-days, not a mean.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from wxmm.analysis.maker_taker import AttributedTrade, ReturnKind, _mean

FLAT_THRESHOLDS: tuple[Decimal, ...] = (Decimal("0.10"), Decimal("0.25"), Decimal("0.50"))


@dataclass(frozen=True, slots=True)
class BracketDayNet:
    ticker: str
    climate_day: date
    contracts_bought: Decimal
    contracts_sold: Decimal
    net_contracts: Decimal
    gross_contracts: Decimal
    abs_net_over_gross: Decimal
    premium_paid: Decimal
    premium_received: Decimal
    net_premium: Decimal
    maker_gross_pnl: Decimal
    maker_net_pnl: Decimal
    maker_mean_net_return: Decimal


class ThresholdShare(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: Decimal
    share_below: Decimal
    n_below: int
    n_bracket_days: int
    maker_mean_net_return: Decimal
    maker_mean_net_pnl: Decimal


class X1bReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReturnKind = "historical_resting_counterparty_return"
    is_strategy_pnl: Literal[False] = False
    n_bracket_days: int
    net_over_gross_p10: Decimal | None
    net_over_gross_p25: Decimal | None
    net_over_gross_p50: Decimal | None
    net_over_gross_p75: Decimal | None
    net_over_gross_p90: Decimal | None
    signed_net_p10: Decimal | None
    signed_net_p50: Decimal | None
    signed_net_p90: Decimal | None
    thresholds: tuple[ThresholdShare, ...]
    near_flat_note: str = (
        "Conditional P&L of bracket-days with |net|/gross below 0.10 is the "
        "closest public-data two-sided-quoting return. Do not average X1a "
        "across directional and flat days."
    )


def _quantile(values: Sequence[Decimal], q: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = q * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = Decimal(str(idx - lo))
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def bracket_day_nets(rows: Sequence[AttributedTrade]) -> list[BracketDayNet]:
    buckets: dict[tuple[str, date], list[AttributedTrade]] = defaultdict(list)
    for row in rows:
        buckets[(row.ticker, row.climate_day)].append(row)
    out: list[BracketDayNet] = []
    for (ticker, climate_day), trades in sorted(buckets.items()):
        bought = Decimal("0")
        sold = Decimal("0")
        paid = Decimal("0")
        received = Decimal("0")
        pnl_g = Decimal("0")
        pnl_n = Decimal("0")
        rets: list[Decimal] = []
        for trade in trades:
            if trade.maker.book_role == "buyer":
                bought += trade.contracts
                paid += trade.maker.notional
            else:
                sold += trade.contracts
                received += trade.maker.notional
            pnl_g += trade.maker.gross_pnl
            pnl_n += trade.maker.net_pnl
            rets.append(trade.maker.net_return)
        gross = bought + sold
        net = bought - sold
        abs_ratio = (abs(net) / gross) if gross else Decimal("0")
        out.append(
            BracketDayNet(
                ticker=ticker,
                climate_day=climate_day,
                contracts_bought=bought,
                contracts_sold=sold,
                net_contracts=net,
                gross_contracts=gross,
                abs_net_over_gross=abs_ratio,
                premium_paid=paid,
                premium_received=received,
                net_premium=received - paid,
                maker_gross_pnl=pnl_g,
                maker_net_pnl=pnl_n,
                maker_mean_net_return=_mean(rets),
            )
        )
    return out


def x1b_report(rows: Sequence[AttributedTrade]) -> X1bReport:
    nets = bracket_day_nets(rows)
    ratios = [row.abs_net_over_gross for row in nets]
    signed = [
        (row.net_contracts / row.gross_contracts) if row.gross_contracts else Decimal("0")
        for row in nets
    ]
    shares: list[ThresholdShare] = []
    n = len(nets)
    for threshold in FLAT_THRESHOLDS:
        below = [row for row in nets if row.abs_net_over_gross < threshold]
        shares.append(
            ThresholdShare(
                threshold=threshold,
                share_below=(Decimal(len(below)) / Decimal(n)) if n else Decimal("0"),
                n_below=len(below),
                n_bracket_days=n,
                maker_mean_net_return=_mean([row.maker_mean_net_return for row in below]),
                maker_mean_net_pnl=_mean([row.maker_net_pnl for row in below]),
            )
        )
    return X1bReport(
        n_bracket_days=n,
        net_over_gross_p10=_quantile(ratios, 0.10),
        net_over_gross_p25=_quantile(ratios, 0.25),
        net_over_gross_p50=_quantile(ratios, 0.50),
        net_over_gross_p75=_quantile(ratios, 0.75),
        net_over_gross_p90=_quantile(ratios, 0.90),
        signed_net_p10=_quantile(signed, 0.10),
        signed_net_p50=_quantile(signed, 0.50),
        signed_net_p90=_quantile(signed, 0.90),
        thresholds=tuple(shares),
    )
