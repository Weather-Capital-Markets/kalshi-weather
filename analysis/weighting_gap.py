"""Paired near-flat weighting gap, and the D4 two-sided count check.

The day-weighted and trade-weighted maker returns on the near-flat universe
are the same fills under two weights. This script resamples climate days
once and applies both weights to each draw, then repeats the gap inside
each price band and after reweighting each bracket-day to the common band
mix. It also counts two-sided book updates with fractional ``count_fp``
kept and dropped.

A hash mismatch against the locked snapshot writes ``status: NOT_RUN`` and
does not score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from analysis.build_labels import read_labels
from wxmm.analysis.maker_taker import (
    PRICE_BANDS,
    AttributedTrade,
    SettlementLabel,
    attribute_trade,
    price_band_of,
)
from wxmm.analysis.population import bracket_day_nets
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    RawTrade,
    ladder_regime_for,
    read_trades_parquet,
)
from wxmm.fairvalue.anchor_trades import yes_space_print
from wxmm.fairvalue.crossed import crossed_state_rate

EXPECTED_TRADES_HASH = "ff7e56e6fc3c4973"
EXPECTED_LABELS_HASH = "05940acd31ce7542"
PUBLISHED_TRADE_WEIGHTED = Decimal("0.03454164279997775390783615123")
PUBLISHED_DAY_WEIGHTED = Decimal("-0.06632376706995207369103787252")
PUBLISHED_N_NEAR_FLAT = 1141
PUBLISHED_N_BRACKET_DAYS = 7571
PUBLISHED_C1_TWO_SIDED = 3368869
PUBLISHED_D4_TWO_SIDED = 3360605
PUBLISHED_TWO_SIDED_GAP = PUBLISHED_C1_TWO_SIDED - PUBLISHED_D4_TWO_SIDED
FLAT_THRESHOLD = Decimal("0.10")
BOOTSTRAP_SEED = 0
BOOTSTRAP_RESAMPLES = 1000
NEAR_FLAT_UNIVERSE = (
    "six_bracket era, labelled, non-block, integer count_fp; "
    "near-flat means |bought-sold|/(bought+sold) < 0.10 on contracts "
    "within (ticker, climate_day). Each print contributes one maker.net_return; "
    "contract size does not weight the mean. Maker fee is $0 on this schedule, "
    "so net_return equals gross_return when the fee check below is zero."
)


def hash_trades(root: Path) -> str:
    """Snapshot id: sorted parquet name, then sha256 of the file bytes."""
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.parquet")):
        digest.update(path.name.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def hash_labels(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _is_fractional(count: Decimal) -> bool:
    return Decimal(int(count)) != count


@dataclass(frozen=True, slots=True)
class BandTotals:
    n: int
    sum_return: Decimal
    sum_cents: Decimal


@dataclass(frozen=True, slots=True)
class FlatBracketDay:
    climate_day: date
    n: int
    sum_return: Decimal
    sum_cents: Decimal
    n_contracts: Decimal
    bands: Mapping[str, BandTotals]


@dataclass
class ScanStats:
    n_not_six: int = 0
    n_before_era: int = 0
    n_unlabelled: int = 0
    n_fractional: int = 0
    n_block: int = 0
    n_primary: int = 0
    n_bracket_days: int = 0
    n_near_flat: int = 0
    n_fee_nonzero: int = 0
    n_net_ne_gross: int = 0


@dataclass(frozen=True, slots=True)
class ClusterDraw:
    """One climate day, already reduced to sums.

    ``sum_values`` / ``n_values`` is the trade-weighted (per-print) side.
    ``sum_unit_means`` / ``n_units`` is the equal-weight-per-bracket-day side.
    Float sums match ``clustered_bootstrap_mean_ci``.
    """

    sum_values: float
    n_values: int
    sum_unit_means: float
    n_units: int


@dataclass(frozen=True, slots=True)
class PairedBootstrap:
    trade_ci: tuple[float, float]
    day_ci: tuple[float, float]
    diff_ci: tuple[float, float]


@dataclass
class D4Tally:
    n_prints: int = 0
    n_two_sided: int = 0
    n_crossed: int = 0
    n_ties: int = 0
    n_strict: int = 0
    n_block_excluded: int = 0
    n_fractional_prints: int = 0
    n_two_sided_triggered_by_fractional: int = 0
    strict_gap_sum: int = 0
    inclusive_gap_sum: int = 0

    def as_dict(self) -> dict[str, int | float | None]:
        mean_strict = (self.strict_gap_sum / self.n_strict) if self.n_strict else None
        n_inclusive = self.n_strict + self.n_ties
        mean_inclusive = (self.inclusive_gap_sum / n_inclusive) if n_inclusive else None
        return {
            "n_prints": self.n_prints,
            "n_two_sided": self.n_two_sided,
            "n_crossed": self.n_crossed,
            "n_ties": self.n_ties,
            "n_strict_uncrossed": self.n_strict,
            "n_block_excluded": self.n_block_excluded,
            "n_fractional_prints": self.n_fractional_prints,
            "n_two_sided_triggered_by_fractional": self.n_two_sided_triggered_by_fractional,
            "mean_uncrossed_cents_strict_ask_gt_bid": mean_strict,
            "mean_uncrossed_cents_inclusive_ask_ge_bid": mean_inclusive,
        }


def points_from_units(units: Sequence[tuple[Decimal, int]]) -> tuple[Decimal, Decimal, Decimal]:
    """Trade-weighted mean, day-weighted mean, and trade minus day.

    Each unit is one bracket-day: ``(sum of per-print values, n prints)``.
    """
    if not units:
        raise ValueError("no units")
    sum_values = sum((total for total, _n in units), Decimal(0))
    n_values = sum(n for _total, n in units)
    if n_values <= 0:
        raise ValueError("unit print count must be positive")
    means = [total / Decimal(n) for total, n in units]
    trade = sum_values / Decimal(n_values)
    day = sum(means, Decimal(0)) / Decimal(len(means))
    return trade, day, trade - day


def cov_gap(units: Sequence[tuple[Decimal, int]]) -> Decimal:
    """``Cov_d(n_d, r_bar_d) / n_bar`` with the population (divide by N) covariance."""
    if not units:
        raise ValueError("no units")
    n_days = len(units)
    ns = [n for _total, n in units]
    means = [total / Decimal(n) for total, n in units]
    n_bar = Decimal(sum(ns)) / Decimal(n_days)
    r_bar = sum(means, Decimal(0)) / Decimal(n_days)
    cov = sum(
        ((Decimal(n) - n_bar) * (mean - r_bar) for n, mean in zip(ns, means, strict=True)),
        Decimal(0),
    ) / Decimal(n_days)
    return cov / n_bar


def mix_return(
    band_means: Mapping[str, Decimal],
    weights: Mapping[str, Decimal],
    *,
    order: Sequence[str],
) -> Decimal:
    """Common-mix return, renormalized over bands the bracket-day actually has."""
    present = [name for name in order if name in band_means]
    if not present:
        raise ValueError("mix return requires at least one band")
    weight_sum = sum((weights[name] for name in present), Decimal(0))
    if weight_sum == 0:
        raise ValueError("mix weights sum to 0 on the bands present")
    weighted = sum((weights[name] * band_means[name] for name in present), Decimal(0))
    return weighted / weight_sum


def percentile_ci(
    samples: list[float],
    *,
    n_resample: int,
    ci: float = 0.95,
) -> tuple[float, float]:
    """Same indices as ``clustered_bootstrap_mean_ci``."""
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    lo = samples[int(math.floor(alpha * n_resample))]
    hi = samples[min(n_resample - 1, int(math.ceil((1.0 - alpha) * n_resample)) - 1)]
    return lo, hi


def clusters_from_units(units: Sequence[tuple[date, Decimal, int]]) -> list[ClusterDraw]:
    buckets: dict[date, list[tuple[Decimal, int]]] = defaultdict(list)
    for climate_day, total, n in units:
        if n <= 0:
            raise ValueError("unit print count must be positive")
        buckets[climate_day].append((total, n))
    clusters: list[ClusterDraw] = []
    for climate_day in sorted(buckets):
        rows = buckets[climate_day]
        sum_values = sum((total for total, _n in rows), Decimal(0))
        n_values = sum(n for _total, n in rows)
        sum_means = sum((total / Decimal(n) for total, n in rows), Decimal(0))
        clusters.append(
            ClusterDraw(
                sum_values=float(sum_values),
                n_values=n_values,
                sum_unit_means=float(sum_means),
                n_units=len(rows),
            )
        )
    return clusters


def paired_bootstrap(
    clusters: Sequence[ClusterDraw],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_resample: int = BOOTSTRAP_RESAMPLES,
    ci: float = 0.95,
) -> PairedBootstrap | None:
    """One climate-day draw, both weights. Difference is trade minus day."""
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    trade: list[float] = []
    day: list[float] = []
    diff: list[float] = []
    n_keys = len(clusters)
    for _ in range(n_resample):
        total_values = 0.0
        n_values = 0
        total_means = 0.0
        n_units = 0
        for _draw in range(n_keys):
            cluster = clusters[rng.randrange(n_keys)]
            total_values += cluster.sum_values
            n_values += cluster.n_values
            total_means += cluster.sum_unit_means
            n_units += cluster.n_units
        trade_mean = total_values / n_values if n_values else 0.0
        day_mean = total_means / n_units if n_units else 0.0
        trade.append(trade_mean)
        day.append(day_mean)
        diff.append(trade_mean - day_mean)
    return PairedBootstrap(
        trade_ci=percentile_ci(trade, n_resample=n_resample, ci=ci),
        day_ci=percentile_ci(day, n_resample=n_resample, ci=ci),
        diff_ci=percentile_ci(diff, n_resample=n_resample, ci=ci),
    )


def _ci_strings(pair: tuple[float, float] | None) -> list[str] | None:
    if pair is None:
        return None
    return [str(Decimal(str(pair[0]))), str(Decimal(str(pair[1])))]


def _includes_or_touches_zero(pair: tuple[float, float] | None) -> bool | None:
    if pair is None:
        return None
    return pair[0] <= 0.0 <= pair[1]


def collect_near_flat(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    stats: ScanStats,
) -> list[FlatBracketDay]:
    """Near-flat bracket-days in ``trades``. Updates ``stats`` in place."""
    by_day: dict[date, list[RawTrade]] = defaultdict(list)
    for trade in trades:
        by_day[trade.climate_day].append(trade)
    found: list[FlatBracketDay] = []
    for climate_day in sorted(by_day):
        found.extend(_one_climate_day(by_day[climate_day], labels, stats))
    return found


def _one_climate_day(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    stats: ScanStats,
) -> list[FlatBracketDay]:
    rows: list[AttributedTrade] = []
    for trade in trades:
        if ladder_regime_for(trade.climate_day) != "six_bracket":
            stats.n_not_six += 1
            continue
        if trade.climate_day < SIX_BRACKET_ERA_START:
            stats.n_before_era += 1
            continue
        label = labels.get(trade.ticker)
        if label is None:
            stats.n_unlabelled += 1
            continue
        if _is_fractional(trade.count):
            stats.n_fractional += 1
            continue
        if trade.is_block_trade:
            stats.n_block += 1
            continue
        row = attribute_trade(trade, label)
        if row.maker.fee != 0:
            stats.n_fee_nonzero += 1
        if row.maker.net_return != row.maker.gross_return:
            stats.n_net_ne_gross += 1
        rows.append(row)
        stats.n_primary += 1
    if not rows:
        return []
    nets = bracket_day_nets(rows)
    stats.n_bracket_days += len(nets)
    flat_keys = {
        (net.ticker, net.climate_day)
        for net in nets
        if net.abs_net_over_gross < FLAT_THRESHOLD
    }
    stats.n_near_flat += len(flat_keys)
    grouped: dict[tuple[str, date], list[AttributedTrade]] = defaultdict(list)
    for row in rows:
        key = (row.ticker, row.climate_day)
        if key in flat_keys:
            grouped[key].append(row)
    out: list[FlatBracketDay] = []
    for key in sorted(grouped):
        prints = grouped[key]
        sum_return = Decimal(0)
        sum_cents = Decimal(0)
        n_contracts = Decimal(0)
        bands: dict[str, list[Decimal]] = defaultdict(list)
        band_cents: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for row in prints:
            maker = row.maker
            y = Decimal(1) if maker.won else Decimal(0)
            cents = (y - maker.price) * Decimal(100)
            sum_return += maker.net_return
            sum_cents += cents
            n_contracts += maker.contracts
            band = price_band_of(maker.price)
            bands[band].append(maker.net_return)
            band_cents[band] += cents
        out.append(
            FlatBracketDay(
                climate_day=key[1],
                n=len(prints),
                sum_return=sum_return,
                sum_cents=sum_cents,
                n_contracts=n_contracts,
                bands={
                    name: BandTotals(
                        n=len(values),
                        sum_return=sum(values, Decimal(0)),
                        sum_cents=band_cents[name],
                    )
                    for name, values in bands.items()
                },
            )
        )
    return out


def replay_d4(trades: Sequence[RawTrade]) -> tuple[D4Tally, D4Tally]:
    """Full replay, and a replay that drops fractional ``count_fp`` before the book updates.

    The full tally's two-sided / crossed / tie counts match ``crossed_state_rate``
    (sign +1, block trades excluded). Dropping a fractional print changes every
    later update on that ticker, so the skip tally is not the full tally minus
    the fractional-triggered rows.
    """
    full = D4Tally()
    skipped = D4Tally()
    by_ticker: dict[str, list[RawTrade]] = defaultdict(list)
    for trade in trades:
        if trade.is_block_trade:
            full.n_block_excluded += 1
            skipped.n_block_excluded += 1
            continue
        by_ticker[trade.ticker].append(trade)
    for ticker_trades in by_ticker.values():
        ordered = sorted(ticker_trades, key=lambda trade: (trade.created_time, trade.trade_id))
        _replay_ticker(ordered, full, skip_fractional=False)
        _replay_ticker(ordered, skipped, skip_fractional=True)
    return full, skipped


def _replay_ticker(trades: Sequence[RawTrade], tally: D4Tally, *, skip_fractional: bool) -> None:
    bid: Decimal | None = None
    ask: Decimal | None = None
    for trade in trades:
        fractional = _is_fractional(trade.count)
        if skip_fractional and fractional:
            continue
        price = yes_space_print(trade).yes_price
        if not skip_fractional and fractional:
            tally.n_fractional_prints += 1
        if trade.taker_outcome_side == "yes":
            ask = price
        else:
            bid = price
        tally.n_prints += 1
        if bid is None or ask is None:
            continue
        tally.n_two_sided += 1
        if not skip_fractional and fractional:
            tally.n_two_sided_triggered_by_fractional += 1
        gap = int(((ask - bid) * Decimal(100)).to_integral_value())
        if ask < bid:
            tally.n_crossed += 1
        elif ask == bid:
            tally.n_ties += 1
            tally.inclusive_gap_sum += gap
        else:
            tally.n_strict += 1
            tally.strict_gap_sum += gap
            tally.inclusive_gap_sum += gap


def assert_full_replay_matches_crossed(trades: Sequence[RawTrade], full: D4Tally) -> None:
    """The full replay is the production crossed accumulator, blocks excluded."""
    rate = crossed_state_rate(trades)
    if (
        full.n_prints != rate.n_prints
        or full.n_two_sided != rate.n_two_sided
        or full.n_crossed != rate.n_crossed
        or full.n_ties != rate.n_touching
    ):
        raise RuntimeError(
            "D4 full replay diverges from crossed_state_rate: "
            f"prints {full.n_prints}/{rate.n_prints} "
            f"two_sided {full.n_two_sided}/{rate.n_two_sided} "
            f"crossed {full.n_crossed}/{rate.n_crossed} "
            f"ties {full.n_ties}/{rate.n_touching}"
        )


def _section(
    units: Sequence[tuple[date, Decimal, int]],
) -> dict[str, object]:
    if not units:
        return {
            "n_prints": 0,
            "n_bracket_days": 0,
            "n_climate_days": 0,
            "trade_weighted": None,
            "day_weighted": None,
            "gap_trade_minus_day": None,
            "identity_cov_over_nbar": None,
            "trade_weighted_ci": None,
            "day_weighted_ci": None,
            "gap_ci": None,
            "gap_ci_includes_or_touches_zero": None,
        }
    trade, day, gap = points_from_units([(total, n) for _climate, total, n in units])
    identity = cov_gap([(total, n) for _climate, total, n in units])
    paired = paired_bootstrap(clusters_from_units(units))
    diff_ci = None if paired is None else paired.diff_ci
    return {
        "n_prints": sum(n for _climate, _total, n in units),
        "n_bracket_days": len(units),
        "n_climate_days": len({climate for climate, _total, _n in units}),
        "trade_weighted": trade,
        "day_weighted": day,
        "gap_trade_minus_day": gap,
        "identity_cov_over_nbar": identity,
        "identity_minus_gap": identity - gap,
        "trade_weighted_ci": None if paired is None else _ci_strings(paired.trade_ci),
        "day_weighted_ci": None if paired is None else _ci_strings(paired.day_ci),
        "gap_ci": _ci_strings(diff_ci),
        "gap_ci_includes_or_touches_zero": _includes_or_touches_zero(diff_ci),
    }


def _band_order() -> tuple[str, ...]:
    return tuple(name for name, _lo, _hi in PRICE_BANDS)


def _global_band_weights(rows: Sequence[FlatBracketDay]) -> dict[str, Decimal]:
    totals = {name: 0 for name in _band_order()}
    for row in rows:
        for name, band in row.bands.items():
            totals[name] += band.n
    grand = sum(totals.values())
    if grand <= 0:
        raise ValueError("near-flat rows have no prints")
    return {name: Decimal(n) / Decimal(grand) for name, n in totals.items()}


def _mix_units(rows: Sequence[FlatBracketDay]) -> list[tuple[date, Decimal, int]]:
    weights = _global_band_weights(rows)
    order = _band_order()
    units: list[tuple[date, Decimal, int]] = []
    for row in rows:
        means = {
            name: band.sum_return / Decimal(band.n)
            for name, band in row.bands.items()
            if band.n
        }
        mixed = mix_return(means, weights, order=order)
        units.append((row.climate_day, mixed * Decimal(row.n), row.n))
    return units


def build_report(
    rows: Sequence[FlatBracketDay],
    stats: ScanStats,
    full: D4Tally,
    skipped: D4Tally,
    *,
    trades_hash: str,
    labels_hash: str,
) -> dict[str, object]:
    return_units = [(row.climate_day, row.sum_return, row.n) for row in rows]
    cent_units = [(row.climate_day, row.sum_cents, row.n) for row in rows]
    overall = _section(return_units)
    cents = _section(cent_units)
    bands = {
        name: _section(
            [
                (row.climate_day, band.sum_return, band.n)
                for row in rows
                if (band := row.bands.get(name)) is not None and band.n
            ]
        )
        for name in _band_order()
    }
    mixed = _section(_mix_units(rows)) if rows else _section([])
    weights = _global_band_weights(rows) if rows else {}
    trade_point = overall["trade_weighted"]
    day_point = overall["day_weighted"]
    delta_two_sided = full.n_two_sided - skipped.n_two_sided
    triggered = full.n_two_sided_triggered_by_fractional
    gap_includes_zero = overall["gap_ci_includes_or_touches_zero"]
    if gap_includes_zero is True:
        reading = (
            "The interval of (trade-weighted - day-weighted) includes or touches zero, "
            "so 'busy days pay makers' is not yet supported."
        )
    elif gap_includes_zero is False:
        reading = (
            "The interval of (trade-weighted - day-weighted) excludes zero. "
            "go_no_go stays FILL_IN."
        )
    else:
        reading = "Paired interval was not computed."
    return {
        "status": "OK",
        "is_strategy_pnl": False,
        "go_no_go": "FILL_IN",
        "snapshot": {
            "trades_hash": trades_hash,
            "labels_hash": labels_hash,
            "expected_trades_hash": EXPECTED_TRADES_HASH,
            "expected_labels_hash": EXPECTED_LABELS_HASH,
        },
        "universe": NEAR_FLAT_UNIVERSE,
        "weighting_unit": "per print",
        "bootstrap": {
            "seed": BOOTSTRAP_SEED,
            "n_resample": BOOTSTRAP_RESAMPLES,
            "cluster": "climate_day",
            "day_order": "sorted climate_day",
            "difference": "trade-weighted minus day-weighted on the same draws",
            "percentile": "floor/ceil indices of clustered_bootstrap_mean_ci",
        },
        "scan": {
            "n_not_six_bracket": stats.n_not_six,
            "n_before_era": stats.n_before_era,
            "n_unlabelled": stats.n_unlabelled,
            "n_fractional_skipped": stats.n_fractional,
            "n_block_skipped": stats.n_block,
            "n_primary_integer": stats.n_primary,
            "n_bracket_days": stats.n_bracket_days,
            "n_near_flat_bracket_days": stats.n_near_flat,
            "n_fee_nonzero": stats.n_fee_nonzero,
            "n_net_return_ne_gross_return": stats.n_net_ne_gross,
            "published_n_near_flat_bracket_days": PUBLISHED_N_NEAR_FLAT,
            "published_n_bracket_days": PUBLISHED_N_BRACKET_DAYS,
            "n_near_flat_matches_published": stats.n_near_flat == PUBLISHED_N_NEAR_FLAT,
            "n_bracket_days_matches_published": stats.n_bracket_days == PUBLISHED_N_BRACKET_DAYS,
        },
        "published_points": {
            "trade_weighted": str(PUBLISHED_TRADE_WEIGHTED),
            "day_weighted": str(PUBLISHED_DAY_WEIGHTED),
            "reproduced_trade_weighted": None if trade_point is None else str(trade_point),
            "reproduced_day_weighted": None if day_point is None else str(day_point),
            "trade_weighted_matches_published": trade_point == PUBLISHED_TRADE_WEIGHTED,
            "day_weighted_matches_published": day_point == PUBLISHED_DAY_WEIGHTED,
        },
        "paired_difference": overall,
        "cents": {
            "definition": (
                "mean of (y - p_maker) * 100, y in {0, 1}, "
                "p_maker the price of the contract the maker is long"
            ),
            "weighting_unit": "per print",
            "n_contracts_on_near_flat_prints": sum((row.n_contracts for row in rows), Decimal(0)),
            "n_prints": sum(row.n for row in rows),
            "paired": cents,
        },
        "within_band": {
            "price": "maker long-contract price, locked PRICE_BANDS",
            "flatness": "still the bracket-day contract flatness, not flatness inside the band",
            "bands": bands,
        },
        "mix_adjusted": {
            "rule": (
                "w_b is the share of near-flat prints in band b. "
                "For each near-flat bracket-day, r_mix = sum_{b in B_d} w_b * r_{d,b} "
                "/ sum_{b in B_d} w_b. Day-weighted is the mean of r_mix. "
                "Trade-weighted is sum n_d * r_mix / sum n_d."
            ),
            "band_weights": weights,
            "paired": mixed,
        },
        "reading": reading,
        "d4_universe": {
            "full_replay": (
                "non-block prints, fractional count_fp kept; "
                "two-sided means both a bid and an ask have printed on the ticker"
            ),
            "skip_fractional_replay": (
                "same replay, but a fractional count_fp is removed before it updates the book, "
                "so later updates on that ticker move"
            ),
            "triggered_by_fractional": (
                "on the full replay, two-sided updates whose own print has a non-integer count_fp"
            ),
            "published_c1_m1_n_two_sided": PUBLISHED_C1_TWO_SIDED,
            "published_d4_n_two_sided": PUBLISHED_D4_TWO_SIDED,
            "published_two_sided_gap": PUBLISHED_TWO_SIDED_GAP,
            "full": full.as_dict(),
            "skip_fractional": skipped.as_dict(),
            "full_minus_skip_fractional_two_sided": delta_two_sided,
            "two_sided_triggered_by_fractional_print": triggered,
            "full_matches_published_c1_two_sided": full.n_two_sided == PUBLISHED_C1_TWO_SIDED,
            "full_matches_published_d4_two_sided": full.n_two_sided == PUBLISHED_D4_TWO_SIDED,
            "skip_matches_published_c1_two_sided": skipped.n_two_sided == PUBLISHED_C1_TWO_SIDED,
            "skip_matches_published_d4_two_sided": skipped.n_two_sided == PUBLISHED_D4_TWO_SIDED,
            "published_gap_equals_full_minus_skip": delta_two_sided == PUBLISHED_TWO_SIDED_GAP,
            "published_gap_equals_triggered_by_fractional": triggered == PUBLISHED_TWO_SIDED_GAP,
        },
    }


def not_run_payload(
    *,
    trades_hash: str,
    labels_hash: str,
    reason: str,
) -> dict[str, object]:
    return {
        "status": "NOT_RUN",
        "reason": reason,
        "is_strategy_pnl": False,
        "go_no_go": "FILL_IN",
        "snapshot": {
            "trades_hash": trades_hash,
            "labels_hash": labels_hash,
            "expected_trades_hash": EXPECTED_TRADES_HASH,
            "expected_labels_hash": EXPECTED_LABELS_HASH,
        },
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonify(payload), indent=2) + "\n", encoding="utf-8")


def _jsonify(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


def run(
    trades_dir: Path,
    labels_path: Path,
    out_path: Path,
    *,
    allow_hash_mismatch: bool = False,
) -> int:
    trades_hash = hash_trades(trades_dir)
    labels_hash = hash_labels(labels_path)
    if trades_hash != EXPECTED_TRADES_HASH or labels_hash != EXPECTED_LABELS_HASH:
        reason = (
            f"snapshot hash mismatch trades={trades_hash} labels={labels_hash}; "
            f"expected trades={EXPECTED_TRADES_HASH} labels={EXPECTED_LABELS_HASH}"
        )
        if not allow_hash_mismatch:
            _write(
                out_path,
                not_run_payload(
                    trades_hash=trades_hash,
                    labels_hash=labels_hash,
                    reason=reason,
                ),
            )
            print(reason, file=sys.stderr)
            return 2
        print(f"continuing past hash mismatch: {reason}", file=sys.stderr)
    labels = read_labels(labels_path)
    stats = ScanStats()
    rows: list[FlatBracketDay] = []
    full = D4Tally()
    skipped = D4Tally()
    shards = sorted(trades_dir.glob("*.parquet"))
    for shard in shards:
        trades = read_trades_parquet(shard)
        print(f"{shard.name} trades={len(trades)}", file=sys.stderr, flush=True)
        rows.extend(collect_near_flat(trades, labels, stats))
        shard_full, shard_skipped = replay_d4(trades)
        assert_full_replay_matches_crossed(trades, shard_full)
        _add_tally(full, shard_full)
        _add_tally(skipped, shard_skipped)
        del trades
    payload = build_report(
        rows,
        stats,
        full,
        skipped,
        trades_hash=trades_hash,
        labels_hash=labels_hash,
    )
    _write(out_path, payload)
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


def _add_tally(total: D4Tally, part: D4Tally) -> None:
    for name in (
        "n_prints",
        "n_two_sided",
        "n_crossed",
        "n_ties",
        "n_strict",
        "n_block_excluded",
        "n_fractional_prints",
        "n_two_sided_triggered_by_fractional",
        "strict_gap_sum",
        "inclusive_gap_sum",
    ):
        setattr(total, name, getattr(total, name) + getattr(part, name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--labels", type=Path, default=Path("data/labels/settlement.json"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("analysis/out/v0_reconcile/weighting_gap.json"),
    )
    parser.add_argument(
        "--allow-hash-mismatch",
        action="store_true",
        help="Score anyway. The default refuses a snapshot other than the locked hashes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        args.trades,
        args.labels,
        args.out,
        allow_hash_mismatch=args.allow_hash_mismatch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
