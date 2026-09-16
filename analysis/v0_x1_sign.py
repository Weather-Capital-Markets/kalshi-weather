"""Memory-light C1-X1 sign-gate on the real trade tape.

``select_primary`` + ``x1a_report`` materialise every attributed trade. On the
3.4M-print corpus that OOM-kills the process (exit 137). This streams parquet
shards, attributes one trade at a time, and keeps only the return series the
sign gate needs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    attribute_trade,
    clustered_bootstrap_mean_ci,
    price_band_of,
    season_of,
)
from wxmm.analysis.population import FLAT_THRESHOLDS, bracket_day_nets
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    RawTrade,
    ladder_regime_for,
    read_trades_parquet,
)


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def stream_sign_gate(
    trades_root: Path,
    labels: dict[str, SettlementLabel],
    *,
    seed: int = 0,
    n_resample: int = 200,
    era_start: date = SIX_BRACKET_ERA_START,
) -> dict[str, Any]:
    shards = (
        sorted(trades_root.glob("*.parquet"))
        if trades_root.is_dir()
        else [trades_root]
    )
    maker_net_by_day: dict[date, list[Decimal]] = defaultdict(list)
    jja_maker_by_band: dict[str, list[Decimal]] = defaultdict(list)
    # Keep attributed rows only long enough to build bracket-day nets for X1b,
    # flushed per climate day once a shard boundary passes that day.
    pending_by_day: dict[date, list[Any]] = defaultdict(list)

    n_primary = 0
    n_block = 0
    n_unlabelled = 0
    n_fractional = 0
    n_seen = 0

    flat_bucket: dict[Decimal, list[Decimal]] = {t: [] for t in FLAT_THRESHOLDS}
    n_bracket_days = 0

    def _flush_days(before: date | None) -> None:
        nonlocal n_bracket_days
        for climate, rows in list(pending_by_day.items()):
            if before is not None and climate >= before:
                continue
            nets = bracket_day_nets(rows)
            n_bracket_days += len(nets)
            for row in nets:
                for threshold in FLAT_THRESHOLDS:
                    if row.abs_net_over_gross < threshold:
                        flat_bucket[threshold].append(row.maker_mean_net_return)
            del pending_by_day[climate]

    for shard in shards:
        trades: list[RawTrade] = read_trades_parquet(shard)
        for trade in trades:
            n_seen += 1
            if ladder_regime_for(trade.climate_day) != "six_bracket":
                continue
            if trade.climate_day < era_start:
                continue
            label = labels.get(trade.ticker)
            if label is None:
                n_unlabelled += 1
                continue
            if Decimal(int(trade.count)) != trade.count:
                n_fractional += 1
                continue
            attributed = attribute_trade(trade, label)
            if trade.is_block_trade:
                n_block += 1
                continue
            n_primary += 1
            maker_net_by_day[trade.climate_day].append(attributed.maker.net_return)
            if season_of(trade.climate_day) == "JJA":
                band = price_band_of(attributed.maker.price)
                jja_maker_by_band[band].append(attributed.maker.net_return)
            pending_by_day[trade.climate_day].append(attributed)
        # Shards are climate_month partitions; flush days that can no longer appear.
        if trades:
            first_day = min(t.climate_day for t in trades)
            _flush_days(first_day)
        del trades

    _flush_days(None)

    all_maker_net = [v for day in maker_net_by_day.values() for v in day]
    maker_ci = clustered_bootstrap_mean_ci(
        maker_net_by_day, seed=seed, n_resample=n_resample
    )
    maker_mean = _mean(all_maker_net)

    jja_slices = []
    for band in sorted(jja_maker_by_band):
        series = jja_maker_by_band[band]
        jja_slices.append(
            {
                "price_band": band,
                "season": "JJA",
                "n_trades": len(series),
                "mean_return": str(_mean(series)) if series else None,
            }
        )

    shares = []
    for threshold in FLAT_THRESHOLDS:
        below = flat_bucket[threshold]
        shares.append(
            {
                "threshold": str(threshold),
                "share_below": (
                    str(Decimal(len(below)) / Decimal(n_bracket_days))
                    if n_bracket_days
                    else "0"
                ),
                "n_below": len(below),
                "n_bracket_days": n_bracket_days,
                "maker_mean_net_return": str(_mean(below)) if below else None,
            }
        )

    return {
        "n_trades_seen": n_seen,
        "n_primary_trades": n_primary,
        "n_block_trades": n_block,
        "n_unlabelled": n_unlabelled,
        "n_fractional_count_fp_skipped": n_fractional,
        "maker_mean_net": str(maker_mean) if maker_mean is not None else None,
        "maker_ci_net": (
            [str(maker_ci[0]), str(maker_ci[1])] if maker_ci else None
        ),
        "maker_ci_excludes_zero_positive": bool(maker_ci and maker_ci[0] > 0),
        "jja_maker_net_slices": jja_slices,
        "x1b_near_flat": {"n_bracket_days": n_bracket_days, "shares": shares},
        "headline": "JJA season-stratified; weather maker fee $0 so gross=net",
        "fractional_note": (
            "Kalshi count_fp can be fractional; fee schedule is per integer "
            "contract so fractional rows are skipped rather than floored"
        ),
    }
