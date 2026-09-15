"""Per-trade maker/taker attribution and realised return to settlement.

Conditioning on a fill is the measurement, not a bug. A maker return is
only observed where someone chose to trade against that quote. That
selection *is* adverse selection. Do not "correct" for it.

This is not a backtest of a strategy we could have run. It measures the
realised returns of whoever was actually resting. We would have been in
a queue behind them and would not have received the same fills. Any
forward-looking claim requires B3's shadow-fill comparator.

Mapping (stated once):
    taker_book_side == ask → taker lifted a resting offer → maker was the seller.
    taker_book_side == bid → taker hit a resting bid → maker was the buyer.
    taker_outcome_side is the contract the taker ended up long; the maker
    holds the complementary outcome at the complementary price.

Returns follow Bürgi, Deng and Whelan (CESifo WP 12122) eqs. (2)–(3):

    r     = (y − p) / p
    r_net = (y − p − c) / (p + c)

on the contract each side is long. Weather maker fee is $0.00; report
gross and net separately because the published −11.99%/−31.46% pool
categories with non-zero maker fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    BookSide,
    OutcomeSide,
    RawTrade,
    ladder_regime_for,
)
from wxmm.backtest.ledger import Ledger, refuse_unless_preregistered
from wxmm.core.errors import GoNoGoNotFilled
from wxmm.core.money import Money
from wxmm.venues.kalshi.fees import FeeRounding, maker_fee, taker_fee

ReturnKind = Literal["historical_resting_counterparty_return"]
PRICE_BANDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("below_10c", Decimal("0.00"), Decimal("0.10")),
    ("c10_25", Decimal("0.10"), Decimal("0.25")),
    ("c25_50", Decimal("0.25"), Decimal("0.50")),
    ("c50_75", Decimal("0.50"), Decimal("0.75")),
    ("c75_90", Decimal("0.75"), Decimal("0.90")),
    ("from_90c", Decimal("0.90"), Decimal("1.01")),
)


def season_of(climate_day: date) -> str:
    """Meteorological season of the climate day. Locked in prereg."""
    month = climate_day.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def price_band_of(price: Decimal) -> str:
    for name, lo, hi in PRICE_BANDS:
        if lo <= price < hi:
            return name
    raise ValueError(f"price {price} outside locked bands")


def complementary(side: OutcomeSide) -> OutcomeSide:
    return "no" if side == "yes" else "yes"


@dataclass(frozen=True, slots=True)
class SettlementLabel:
    """CLINYC-as-issued resolution of one ticker. Not METAR, not GHCN-D."""

    ticker: str
    climate_day: date
    yes_won: bool
    source: Literal["clinyc_as_issued"]


class SideReturn(BaseModel):
    """Return of one historical counterparty. Not strategy P&L."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReturnKind = "historical_resting_counterparty_return"
    is_strategy_pnl: Literal[False] = False
    role: Literal["maker", "taker"]
    outcome_side: OutcomeSide
    book_role: Literal["buyer", "seller"]
    price: Decimal
    contracts: Decimal
    won: bool
    gross_pnl: Decimal
    net_pnl: Decimal
    fee: Decimal
    gross_return: Decimal
    net_return: Decimal
    notional: Decimal


class AttributedTrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReturnKind = "historical_resting_counterparty_return"
    is_strategy_pnl: Literal[False] = False
    trade_id: str
    ticker: str
    climate_day: date
    season: str
    ladder_regime: str
    settlement_rule_id: str
    close_time_convention: str
    is_block_trade: bool
    taker_outcome_side: OutcomeSide
    taker_book_side: BookSide
    yes_price: Decimal
    no_price: Decimal
    contracts: Decimal
    yes_won: bool
    taker: SideReturn
    maker: SideReturn
    fee_rounding: str


def _outcome_price(trade: RawTrade, side: OutcomeSide) -> Decimal:
    return trade.yes_price if side == "yes" else trade.no_price


def _won(yes_won: bool, side: OutcomeSide) -> bool:
    return yes_won if side == "yes" else (not yes_won)


def _side_return(
    *,
    role: Literal["maker", "taker"],
    outcome_side: OutcomeSide,
    book_role: Literal["buyer", "seller"],
    price: Decimal,
    contracts: Decimal,
    won: bool,
    fee: Money,
) -> SideReturn:
    y = Decimal("1") if won else Decimal("0")
    count = contracts
    gross_pnl = count * (y - price)
    fee_amt = fee.amount
    net_pnl = gross_pnl - fee_amt
    c_per = (fee_amt / count) if count else Decimal("0")
    if price == 0:
        raise ValueError("price 0: return undefined")
    gross_return = (y - price) / price
    net_return = (y - price - c_per) / (price + c_per)
    return SideReturn(
        role=role,
        outcome_side=outcome_side,
        book_role=book_role,
        price=price,
        contracts=count,
        won=won,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fee=fee_amt,
        gross_return=gross_return,
        net_return=net_return,
        notional=count * price,
    )


def attribute_trade(
    trade: RawTrade,
    label: SettlementLabel,
    *,
    rounding: FeeRounding = FeeRounding.PER_ORDER_CENT,
) -> AttributedTrade:
    if trade.ticker != label.ticker:
        raise ValueError(f"label ticker {label.ticker!r} != trade {trade.ticker!r}")
    if trade.count <= 0:
        raise ValueError("count must be positive")
    contracts_int = int(trade.count)
    if Decimal(contracts_int) != trade.count:
        # Fee schedule is per integer contract; refuse fractional mystery sizes.
        raise ValueError(f"non-integer count_fp={trade.count}")

    taker_outcome: OutcomeSide = trade.taker_outcome_side
    maker_outcome = complementary(taker_outcome)
    taker_price = _outcome_price(trade, taker_outcome)
    maker_price = _outcome_price(trade, maker_outcome)
    taker_book_role: Literal["buyer", "seller"] = (
        "buyer" if trade.taker_book_side == "ask" else "seller"
    )
    maker_book_role: Literal["buyer", "seller"] = (
        "seller" if trade.taker_book_side == "ask" else "buyer"
    )
    # taker_book_side == ask → maker seller; bid → maker buyer. Cross-check:
    if trade.taker_book_side == "ask" and maker_book_role != "seller":
        raise RuntimeError("mapping invariant failed")
    if trade.taker_book_side == "bid" and maker_book_role != "buyer":
        raise RuntimeError("mapping invariant failed")

    t_fee = taker_fee(contracts_int, taker_price, rounding)
    m_fee = maker_fee(contracts_int, maker_price)
    if m_fee != Money.zero():
        raise RuntimeError("weather maker fee must be $0.00")

    taker = _side_return(
        role="taker",
        outcome_side=taker_outcome,
        book_role=taker_book_role,
        price=taker_price,
        contracts=trade.count,
        won=_won(label.yes_won, taker_outcome),
        fee=t_fee,
    )
    maker = _side_return(
        role="maker",
        outcome_side=maker_outcome,
        book_role=maker_book_role,
        price=maker_price,
        contracts=trade.count,
        won=_won(label.yes_won, maker_outcome),
        fee=m_fee,
    )
    if taker.gross_pnl + maker.gross_pnl != Decimal("0"):
        raise RuntimeError("gross P&Ls must sum to zero")
    if taker.net_pnl + maker.net_pnl != -(taker.fee + maker.fee):
        raise RuntimeError("net P&Ls must sum to −(fees)")

    return AttributedTrade(
        trade_id=trade.trade_id,
        ticker=trade.ticker,
        climate_day=trade.climate_day,
        season=season_of(trade.climate_day),
        ladder_regime=trade.ladder_regime,
        settlement_rule_id=trade.settlement_rule_id,
        close_time_convention=trade.close_time_convention,
        is_block_trade=trade.is_block_trade,
        taker_outcome_side=trade.taker_outcome_side,
        taker_book_side=trade.taker_book_side,
        yes_price=trade.yes_price,
        no_price=trade.no_price,
        contracts=trade.count,
        yes_won=label.yes_won,
        taker=taker,
        maker=maker,
        fee_rounding=rounding.value,
    )


@dataclass(slots=True, frozen=True)
class CompactFill:
    """Attributed fill without nested Pydantic models. Shard-safe corpus path."""

    climate_day: date
    ticker: str
    season: str
    is_block: bool
    taker_book_side: BookSide
    yes_price: float
    yes_won: bool
    contracts: float
    maker_buyer: bool
    maker_price: float
    maker_gross: float
    maker_net: float
    maker_notional: float
    maker_gross_pnl: float
    maker_net_pnl: float
    taker_price: float
    taker_gross: float
    taker_net: float
    taker_notional: float


def compact_from_attributed(row: AttributedTrade) -> CompactFill:
    return CompactFill(
        climate_day=row.climate_day,
        ticker=row.ticker,
        season=row.season,
        is_block=row.is_block_trade,
        taker_book_side=row.taker_book_side,
        yes_price=float(row.yes_price),
        yes_won=row.yes_won,
        contracts=float(row.contracts),
        maker_buyer=row.maker.book_role == "buyer",
        maker_price=float(row.maker.price),
        maker_gross=float(row.maker.gross_return),
        maker_net=float(row.maker.net_return),
        maker_notional=float(row.maker.notional),
        maker_gross_pnl=float(row.maker.gross_pnl),
        maker_net_pnl=float(row.maker.net_pnl),
        taker_price=float(row.taker.price),
        taker_gross=float(row.taker.gross_return),
        taker_net=float(row.taker.net_return),
        taker_notional=float(row.taker.notional),
    )


class CellStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n_trades: int
    n_contracts: Decimal
    notional: Decimal
    mean_return: Decimal
    median_return: Decimal
    ci_low: Decimal | None
    ci_high: Decimal | None


class SliceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price_band: str
    season: str
    side: Literal["maker", "taker"]
    net_of_fee: bool
    stats: CellStats


class X1aReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReturnKind = "historical_resting_counterparty_return"
    is_strategy_pnl: Literal[False] = False
    n_primary_trades: int
    n_block_trades: int
    n_unlabelled: int
    maker_mean_gross: Decimal
    maker_mean_net: Decimal
    maker_median_gross: Decimal
    maker_median_net: Decimal
    taker_mean_gross: Decimal
    taker_mean_net: Decimal
    taker_median_gross: Decimal
    taker_median_net: Decimal
    maker_ci_net: tuple[Decimal, Decimal] | None
    taker_ci_net: tuple[Decimal, Decimal] | None
    gap_gross: Decimal
    gap_net: Decimal
    fee_attributable_gap: Decimal
    slices: tuple[SliceRow, ...]
    block_maker_mean_net: Decimal | None
    note: str = (
        "Historical resting counterparties, not a strategy we could have run. "
        "Conditioning on a fill is adverse selection."
    )


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _cell(
    returns: Sequence[Decimal],
    contracts: Sequence[Decimal],
    notionals: Sequence[Decimal],
    ci: tuple[Decimal, Decimal] | None,
) -> CellStats:
    return CellStats(
        n_trades=len(returns),
        n_contracts=sum(contracts, Decimal("0")),
        notional=sum(notionals, Decimal("0")),
        mean_return=_mean(returns),
        median_return=_median(returns),
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
    )


def _rng(seed: int) -> Any:
    import random

    return random.Random(seed)


def bootstrap_mean_ci(
    values: Sequence[Decimal],
    *,
    seed: int,
    n_resample: int = 1000,
    ci: float = 0.95,
) -> tuple[Decimal, Decimal] | None:
    if len(values) < 2:
        return None
    rng = _rng(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_resample):
        draw = [values[rng.randrange(n)] for _ in range(n)]
        means.append(float(_mean(draw)))
    means.sort()
    alpha = (1.0 - ci) / 2.0
    lo = means[int(math.floor(alpha * n_resample))]
    hi = means[min(n_resample - 1, int(math.ceil((1.0 - alpha) * n_resample)) - 1)]
    return Decimal(str(lo)), Decimal(str(hi))


def clustered_bootstrap_mean_ci(
    groups: Mapping[date, Sequence[Decimal]],
    *,
    seed: int,
    n_resample: int = 1000,
    ci: float = 0.95,
) -> tuple[Decimal, Decimal] | None:
    """Resample climate days, not trades. Six brackets on one day are one draw.

    Day sums and counts are sufficient for the mean: drawing a day twice
    includes its trades twice, which is ``(sum + sum) / (n + n)``.
    """
    keys = list(groups)
    if len(keys) < 2:
        return None
    day_sum = {key: float(sum(groups[key], Decimal("0"))) for key in keys}
    day_n = {key: len(groups[key]) for key in keys}
    rng = _rng(seed)
    means: list[float] = []
    n_keys = len(keys)
    for _ in range(n_resample):
        tot = 0.0
        n = 0
        for _draw in range(n_keys):
            day = keys[rng.randrange(n_keys)]
            tot += day_sum[day]
            n += day_n[day]
        means.append(tot / n if n else 0.0)
    means.sort()
    alpha = (1.0 - ci) / 2.0
    lo = means[int(math.floor(alpha * n_resample))]
    hi = means[min(n_resample - 1, int(math.ceil((1.0 - alpha) * n_resample)) - 1)]
    return Decimal(str(lo)), Decimal(str(hi))


def _group_by_day(
    rows: Sequence[AttributedTrade],
    *,
    side: Literal["maker", "taker"],
    net: bool,
) -> dict[date, list[Decimal]]:
    out: dict[date, list[Decimal]] = {}
    for row in rows:
        ret = row.maker if side == "maker" else row.taker
        value = ret.net_return if net else ret.gross_return
        out.setdefault(row.climate_day, []).append(value)
    return out


def _returns(
    rows: Sequence[AttributedTrade],
    *,
    side: Literal["maker", "taker"],
    net: bool,
) -> list[Decimal]:
    out: list[Decimal] = []
    for row in rows:
        ret = row.maker if side == "maker" else row.taker
        out.append(ret.net_return if net else ret.gross_return)
    return out


def select_primary(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    era_start: date = SIX_BRACKET_ERA_START,
) -> tuple[list[AttributedTrade], list[AttributedTrade], int]:
    """Primary = non-block, six-bracket era, labelled. Blocks reported separately."""
    primary: list[AttributedTrade] = []
    blocks: list[AttributedTrade] = []
    n_unlabelled = 0
    for trade in trades:
        if ladder_regime_for(trade.climate_day) != "six_bracket":
            continue
        if trade.climate_day < era_start:
            continue
        label = labels.get(trade.ticker)
        if label is None:
            n_unlabelled += 1
            continue
        attributed = attribute_trade(trade, label)
        if trade.is_block_trade:
            blocks.append(attributed)
        else:
            primary.append(attributed)
    return primary, blocks, n_unlabelled


def select_primary_compact(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    era_start: date = SIX_BRACKET_ERA_START,
) -> tuple[list[CompactFill], list[CompactFill], int, int]:
    """Same selection as ``select_primary``, dropping nested Pydantic models."""
    primary: list[CompactFill] = []
    blocks: list[CompactFill] = []
    n_unlabelled = 0
    n_skipped = 0
    for trade in trades:
        if ladder_regime_for(trade.climate_day) != "six_bracket":
            continue
        if trade.climate_day < era_start:
            continue
        label = labels.get(trade.ticker)
        if label is None:
            n_unlabelled += 1
            continue
        try:
            attributed = attribute_trade(trade, label)
        except ValueError:
            n_skipped += 1
            continue
        compact = compact_from_attributed(attributed)
        if trade.is_block_trade:
            blocks.append(compact)
        else:
            primary.append(compact)
    return primary, blocks, n_unlabelled, n_skipped


def _compact_returns(
    rows: Sequence[CompactFill],
    *,
    side: Literal["maker", "taker"],
    net: bool,
) -> list[Decimal]:
    out: list[Decimal] = []
    for row in rows:
        if side == "maker":
            value = row.maker_net if net else row.maker_gross
        else:
            value = row.taker_net if net else row.taker_gross
        out.append(Decimal(str(value)))
    return out


def _group_compact_by_day(
    rows: Sequence[CompactFill],
    *,
    side: Literal["maker", "taker"],
    net: bool,
) -> dict[date, list[Decimal]]:
    out: dict[date, list[Decimal]] = {}
    for row in rows:
        if side == "maker":
            value = row.maker_net if net else row.maker_gross
        else:
            value = row.taker_net if net else row.taker_gross
        out.setdefault(row.climate_day, []).append(Decimal(str(value)))
    return out


def x1a_report(
    primary: Sequence[AttributedTrade],
    blocks: Sequence[AttributedTrade],
    *,
    n_unlabelled: int,
    seed: int,
    n_resample: int = 1000,
) -> X1aReport:
    maker_g = _returns(primary, side="maker", net=False)
    maker_n = _returns(primary, side="maker", net=True)
    taker_g = _returns(primary, side="taker", net=False)
    taker_n = _returns(primary, side="taker", net=True)
    maker_ci = clustered_bootstrap_mean_ci(
        _group_by_day(primary, side="maker", net=True), seed=seed, n_resample=n_resample
    )
    taker_ci = clustered_bootstrap_mean_ci(
        _group_by_day(primary, side="taker", net=True), seed=seed + 1, n_resample=n_resample
    )
    gap_gross = _mean(maker_g) - _mean(taker_g)
    gap_net = _mean(maker_n) - _mean(taker_n)
    slices: list[SliceRow] = []
    for band, _lo, _hi in PRICE_BANDS:
        for season in ("DJF", "MAM", "JJA", "SON"):
            for side in ("maker", "taker"):
                for net in (False, True):
                    cell_rows = [
                        row
                        for row in primary
                        if season_of(row.climate_day) == season
                        and price_band_of(
                            row.maker.price if side == "maker" else row.taker.price
                        )
                        == band
                    ]
                    rets = _returns(cell_rows, side=side, net=net)
                    contracts = [row.contracts for row in cell_rows]
                    notionals = [
                        (row.maker.notional if side == "maker" else row.taker.notional)
                        for row in cell_rows
                    ]
                    slices.append(
                        SliceRow(
                            price_band=band,
                            season=season,
                            side=side,
                            net_of_fee=net,
                            stats=_cell(rets, contracts, notionals, None),
                        )
                    )
    block_mean = (
        _mean(_returns(blocks, side="maker", net=True)) if blocks else None
    )
    return X1aReport(
        n_primary_trades=len(primary),
        n_block_trades=len(blocks),
        n_unlabelled=n_unlabelled,
        maker_mean_gross=_mean(maker_g),
        maker_mean_net=_mean(maker_n),
        maker_median_gross=_median(maker_g),
        maker_median_net=_median(maker_n),
        taker_mean_gross=_mean(taker_g),
        taker_mean_net=_mean(taker_n),
        taker_median_gross=_median(taker_g),
        taker_median_net=_median(taker_n),
        maker_ci_net=maker_ci,
        taker_ci_net=taker_ci,
        gap_gross=gap_gross,
        gap_net=gap_net,
        fee_attributable_gap=gap_net - gap_gross,
        slices=tuple(slices),
        block_maker_mean_net=block_mean,
    )


def x1a_report_compact(
    primary: Sequence[CompactFill],
    blocks: Sequence[CompactFill],
    *,
    n_unlabelled: int,
    seed: int,
    n_resample: int = 1000,
) -> X1aReport:
    maker_g = _compact_returns(primary, side="maker", net=False)
    maker_n = _compact_returns(primary, side="maker", net=True)
    taker_g = _compact_returns(primary, side="taker", net=False)
    taker_n = _compact_returns(primary, side="taker", net=True)
    maker_ci = clustered_bootstrap_mean_ci(
        _group_compact_by_day(primary, side="maker", net=True),
        seed=seed,
        n_resample=n_resample,
    )
    taker_ci = clustered_bootstrap_mean_ci(
        _group_compact_by_day(primary, side="taker", net=True),
        seed=seed + 1,
        n_resample=n_resample,
    )
    gap_gross = _mean(maker_g) - _mean(taker_g)
    gap_net = _mean(maker_n) - _mean(taker_n)
    buckets: dict[tuple[str, str, str], list[CompactFill]] = {}
    for row in primary:
        maker_band = price_band_of(Decimal(f"{row.maker_price:.4f}"))
        taker_band = price_band_of(Decimal(f"{row.taker_price:.4f}"))
        buckets.setdefault((maker_band, row.season, "maker"), []).append(row)
        buckets.setdefault((taker_band, row.season, "taker"), []).append(row)
    slices: list[SliceRow] = []
    for band, _lo, _hi in PRICE_BANDS:
        for season in ("DJF", "MAM", "JJA", "SON"):
            for side in ("maker", "taker"):
                cell_rows = buckets.get((band, season, side), [])
                for net in (False, True):
                    rets = _compact_returns(cell_rows, side=side, net=net)
                    contracts = [Decimal(str(row.contracts)) for row in cell_rows]
                    notionals = [
                        Decimal(
                            str(row.maker_notional if side == "maker" else row.taker_notional)
                        )
                        for row in cell_rows
                    ]
                    slices.append(
                        SliceRow(
                            price_band=band,
                            season=season,
                            side=side,
                            net_of_fee=net,
                            stats=_cell(rets, contracts, notionals, None),
                        )
                    )
    block_mean = (
        _mean(_compact_returns(blocks, side="maker", net=True)) if blocks else None
    )
    return X1aReport(
        n_primary_trades=len(primary),
        n_block_trades=len(blocks),
        n_unlabelled=n_unlabelled,
        maker_mean_gross=_mean(maker_g),
        maker_mean_net=_mean(maker_n),
        maker_median_gross=_median(maker_g),
        maker_median_net=_median(maker_n),
        taker_mean_gross=_mean(taker_g),
        taker_mean_net=_mean(taker_n),
        taker_median_gross=_median(taker_g),
        taker_median_net=_median(taker_n),
        maker_ci_net=maker_ci,
        taker_ci_net=taker_ci,
        gap_gross=gap_gross,
        gap_net=gap_net,
        fee_attributable_gap=gap_net - gap_gross,
        slices=tuple(slices),
        block_maker_mean_net=block_mean,
    )


def assert_go_no_go_filled(prereg: Mapping[str, Any]) -> None:
    gng = prereg.get("go_no_go") or {}
    missing = [key for key, value in gng.items() if value == "FILL_IN" or value is None]
    if missing:
        raise GoNoGoNotFilled(
            "prereg go_no_go still FILL_IN: "
            + ", ".join(missing)
            + "; numbers come from the root chat, do not invent them"
        )


def run_x1a(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    prereg: Mapping[str, Any],
    prereg_dir: Any,
    ledger: Ledger,
    n_resample: int = 1000,
) -> X1aReport:
    registered = refuse_unless_preregistered(dict(prereg), prereg_dir)
    assert_go_no_go_filled(registered)
    primary, blocks, n_unlabelled = select_primary(trades, labels)
    report = x1a_report(
        primary,
        blocks,
        n_unlabelled=n_unlabelled,
        seed=int(registered.get("seed", 0)),
        n_resample=n_resample,
    )
    ledger.record(
        "C1_X1A",
        {
            "prereg_id": registered["prereg_id"],
            "n_primary": report.n_primary_trades,
            "is_strategy_pnl": False,
        },
    )
    return report
