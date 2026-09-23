"""C1-M2 part 2: three-way era, common-subset decomposition, horizon grid.

Retention is cents per contract. Both weightings on every headline.
Fractional count_fp is included as Decimal size. X1 go_no_go stays FILL_IN.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from analysis.build_labels import read_labels
from analysis.c1_m2_run import AS_OF, CLINYC, LABELS, TRADES, _revision_audit
from wxmm.analysis.maker_taker import season_of
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    RawTrade,
    ladder_regime_for,
    read_trades_parquet,
)
from wxmm.backtest.ledger import config_hash
from wxmm.fairvalue.anchor_trades import yes_space_print
from wxmm.measure.c1_m2 import (
    CLOSE_TIME_CONVENTION_CHANGE,
    MM_PROGRAM_SCHEDULED_END,
    MM_PROGRAM_START,
    POST_PROGRAM_CAUTIONS,
    MarkedFill,
    SettlementFill,
    TapePrint,
    both_weightings,
    common_subset_decomposition,
    era_block,
    horizon_volume_grid,
    maker_concentration_not_run,
    mm_era_of,
    replay_marks,
    summarise_retention,
)

OUT = Path("analysis/out/c1_m2_era")
PREREG = Path("prereg/c1-m2-era.yaml")
N_RESAMPLE = 1000


def _float_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _load_prereg() -> tuple[dict[str, Any], str]:
    payload = yaml.safe_load(PREREG.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"prereg at {PREREG} is not a mapping")
    return payload, config_hash(payload)


def _replay_ticker(trades: list[RawTrade]) -> tuple[MarkedFill, ...]:
    prints: list[TapePrint] = []
    for trade in trades:
        if trade.is_block_trade or trade.count <= 0:
            continue
        yes = yes_space_print(trade)
        prints.append(
            TapePrint(
                ts=trade.created_time,
                yes_price=yes.yes_price,
                direction=yes.direction,
                count=trade.count,
                trade_id=trade.trade_id,
            )
        )
    return replay_marks(prints)


def _is_fractional(count: Decimal) -> bool:
    return Decimal(int(count)) != count


def _weighting_block(est: Any) -> dict[str, Any]:
    return {
        "point_cents_per_contract": est.point,
        "ci_low_cents_per_contract": est.ci_low,
        "ci_high_cents_per_contract": est.ci_high,
        "n_fills": est.n,
        "n_days": est.n_days,
        "universe": "full_sample",
        "unit": "cents_per_contract",
    }


def run() -> dict[str, Any]:
    prereg, prereg_hash = _load_prereg()
    if not TRADES.is_dir() or not any(TRADES.glob("*.parquet")):
        return {
            "status": "NOT_RUN",
            "reason": f"no trade parquet under {TRADES}",
            "prereg_id": prereg.get("prereg_id"),
            "prereg_hash": prereg_hash,
        }
    if not LABELS.exists():
        return {
            "status": "NOT_RUN",
            "reason": f"no labels at {LABELS}",
            "prereg_id": prereg.get("prereg_id"),
            "prereg_hash": prereg_hash,
        }

    labels = read_labels(LABELS)
    labelled_days = {label.climate_day for label in labels.values()}
    revisions = (
        _revision_audit(CLINYC, labelled_days)
        if CLINYC.exists()
        else {"status": "NOT_RUN", "reason": f"no CLINYC at {CLINYC}"}
    )

    fills: list[SettlementFill] = []
    n_primary = 0
    n_fractional = 0
    n_block = 0
    n_unlabelled = 0
    n_unattributed = 0
    n_nonpositive = 0
    fractional_premium = 0.0
    integer_premium = 0.0
    unattributed_premium = 0.0
    post_premium = 0.0
    post_unattributed_premium = 0.0
    fractional_first: date | None = None
    fractional_last: date | None = None
    effective_by_day: dict[date, list[float]] = defaultdict(list)
    h30_by_day: dict[date, list[float]] = defaultdict(list)

    for shard in sorted(TRADES.glob("*.parquet")):
        by_ticker: dict[str, list[RawTrade]] = defaultdict(list)
        for trade in read_trades_parquet(shard):
            if ladder_regime_for(trade.climate_day) != "six_bracket":
                continue
            if trade.climate_day < SIX_BRACKET_ERA_START:
                continue
            if trade.is_block_trade:
                n_block += 1
                continue
            label = labels.get(trade.ticker)
            if label is None:
                n_unlabelled += 1
                continue
            premium = float(trade.yes_price * trade.count)
            era = mm_era_of(label.climate_day)
            if era == "post_mm_program":
                post_premium += premium
            if trade.count <= 0:
                n_nonpositive += 1
                n_unattributed += 1
                unattributed_premium += premium
                if era == "post_mm_program":
                    post_unattributed_premium += premium
                continue
            if _is_fractional(trade.count):
                n_fractional += 1
                fractional_premium += premium
                if fractional_first is None or label.climate_day < fractional_first:
                    fractional_first = label.climate_day
                if fractional_last is None or label.climate_day > fractional_last:
                    fractional_last = label.climate_day
            else:
                integer_premium += premium
            by_ticker[trade.ticker].append(trade)

        for ticker, rows in by_ticker.items():
            label = labels[ticker]
            marked = {row.trade_id: row for row in _replay_ticker(rows)}
            payoff = Decimal("1") if label.yes_won else Decimal("0")
            season = season_of(label.climate_day)
            era = mm_era_of(label.climate_day)
            for row in rows:
                fill = marked.get(row.trade_id)
                if fill is None:
                    n_unattributed += 1
                    lost = float(row.yes_price * row.count)
                    unattributed_premium += lost
                    if era == "post_mm_program":
                        post_unattributed_premium += lost
                    continue
                retention = fill.realised_cents(payoff)
                if retention is None:
                    n_unattributed += 1
                    lost = float(row.yes_price * row.count)
                    unattributed_premium += lost
                    if era == "post_mm_program":
                        post_unattributed_premium += lost
                    continue
                n_primary += 1
                record = SettlementFill(
                    climate_day=label.climate_day,
                    season=season,
                    mm_era=era,
                    retention_cents=float(retention),
                    effective_cents=_float_or_none(fill.effective_cents()),
                    premium=float(row.yes_price * row.count),
                    count=float(row.count),
                    realised_1m=_float_or_none(fill.realised_cents(fill.mark_1m_strict)),
                    realised_5m=_float_or_none(fill.realised_cents(fill.mark_5m_strict)),
                    realised_30m=_float_or_none(
                        fill.realised_cents(fill.mark_30m_strict)
                    ),
                )
                fills.append(record)
                if record.effective_cents is not None:
                    effective_by_day[record.climate_day].append(record.effective_cents)
                if record.realised_30m is not None:
                    h30_by_day[record.climate_day].append(record.realised_30m)
        del by_ticker

    premium_by_day: dict[date, float] = defaultdict(float)
    for fill in fills:
        premium_by_day[fill.climate_day] += fill.premium
    common = [fill for fill in fills if fill.in_common_subset()]
    full_settlement = summarise_retention(fills, seed=0, n_resample=N_RESAMPLE)
    eras = {
        "pre_mm_program": era_block(
            fills, "pre_mm_program", seed=100, n_resample=N_RESAMPLE
        ),
        "mm_program": era_block(fills, "mm_program", seed=200, n_resample=N_RESAMPLE),
        "post_mm_program": era_block(
            fills, "post_mm_program", seed=300, n_resample=N_RESAMPLE
        ),
    }
    post = eras["post_mm_program"]
    common_decomp = common_subset_decomposition(
        fills, seed=400, n_resample=N_RESAMPLE
    )
    grid = horizon_volume_grid(
        common,
        premium_by_day=dict(premium_by_day),
        seed=500,
        n_resample=N_RESAMPLE,
    )
    kept_premium = fractional_premium + integer_premium
    return {
        "status": "OK",
        "unit": "cents_per_contract",
        "prereg_id": prereg.get("prereg_id"),
        "prereg_hash": prereg_hash,
        "prereg_path": str(PREREG),
        "uncrossed_definition": "ask > bid; ties excluded from the strict touch mid",
        "mm_program_start": MM_PROGRAM_START.isoformat(),
        "mm_program_scheduled_end": MM_PROGRAM_SCHEDULED_END.isoformat(),
        "close_time_convention_change": CLOSE_TIME_CONVENTION_CHANGE.isoformat(),
        "post_program_cautions": list(POST_PROGRAM_CAUTIONS),
        "mm_program_source": (
            "CFTC Kalshi Market Maker Program terms, effective 2024-03-11 "
            "through 2026-03-11 unless extended (rules02262412176). "
            "Designated-contract scope is not verified for every KXHIGHNY "
            "bracket. Post-program status is PROGRAM_STATUS_UNVERIFIED."
        ),
        "label_noise_definition": (
            "venue disagreement, data-sources.md §1.3.1; not recomputed here"
        ),
        "edt_midnight_1am_carried_from_run_A": 52,
        "x1_go_no_go": "FILL_IN",
        "n_primary_fills": n_primary,
        "n_block_excluded": n_block,
        "n_unlabelled_excluded": n_unlabelled,
        "fractional_count_fp": {
            "handling": "included_as_decimal",
            "n_included": n_fractional,
            "n_unattributed": n_unattributed,
            "n_nonpositive": n_nonpositive,
            "premium_fractional": fractional_premium,
            "premium_integer": integer_premium,
            "premium_share_fractional": (
                fractional_premium / kept_premium if kept_premium else None
            ),
            "first_climate_day": (
                fractional_first.isoformat() if fractional_first else None
            ),
            "last_climate_day": (
                fractional_last.isoformat() if fractional_last else None
            ),
            "unattributed_premium": unattributed_premium,
            "post_program_premium": post_premium,
            "post_program_unattributed_premium": post_unattributed_premium,
            "post_program_unattributed_premium_share": (
                post_unattributed_premium / post_premium if post_premium else None
            ),
            "note": (
                "Non-integer count_fp is economic size (min 0.01 contracts). "
                "Included as Decimal. Not floored, rounded, or scaled. "
                "Trade-weighted remains per-print."
            ),
            "source": (
                "https://docs.kalshi.com/getting_started/fixed_point_migration "
                "(last updated 2026-08-20)"
            ),
        },
        "revision_detection": revisions,
        "full_sample": {
            "universe": "full_sample",
            "note": (
                "Different fill sets from the common subset. Do not subtract "
                "effective, 30m, and settlement across these universes."
            ),
            "settlement_retention": full_settlement,
            "effective_half_spread_strict_mid": {
                key: _weighting_block(est)
                for key, est in both_weightings(
                    effective_by_day, seed=7, n_resample=N_RESAMPLE
                ).items()
            },
            "realised_30m_strict_mid": {
                key: _weighting_block(est)
                for key, est in both_weightings(
                    h30_by_day, seed=8, n_resample=N_RESAMPLE
                ).items()
            },
        },
        "by_mm_era": eras,
        "sign_test_post_mm_program": {
            "n_fills": post["n_fills"],
            "n_days": post["n_days"],
            "sum_count": post["sum_count"],
            "premium": post["premium"],
            "program_status": post["program_status"],
            "cautions": post.get("cautions"),
            "sign": post["sign"],
            "all_seasons": post["all_seasons"],
            "jja": post["jja"],
            "gate0_all_seasons": post["gate0_all_seasons"],
            "root_chat": _root_chat_flag(post),
        },
        "common_subset": common_decomp,
        "horizon_volume_grid": grid,
        "maker_concentration": maker_concentration_not_run(),
        "as_of": AS_OF.isoformat(),
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


def _root_chat_flag(post: dict[str, object]) -> dict[str, object]:
    sign = post["sign"]
    assert isinstance(sign, dict)
    combined = str(sign["combined"])
    take = combined == "positive_excluding_zero"
    return {
        "take_implied_share_to_root_chat": take,
        "reading": combined,
        "note": (
            "Take the implied Gate 0 share to the root chat only when "
            "post-program retention is positive with the day-clustered CI "
            "excluding zero under both weightings."
        ),
    }


def _fmt_est(est: dict[str, Any]) -> str:
    point = est.get("point_cents_per_contract")
    lo = est.get("ci_low_cents_per_contract")
    hi = est.get("ci_high_cents_per_contract")
    if point is None:
        return "NOT_RUN"
    lo_s = f"{lo:.3f}" if isinstance(lo, (int, float)) else "NA"
    hi_s = f"{hi:.3f}" if isinstance(hi, (int, float)) else "NA"
    return f"{point:.3f}¢  [{lo_s}, {hi_s}]"


def render_text(payload: dict[str, Any]) -> str:
    if payload.get("status") != "OK":
        return json.dumps(payload, indent=2, default=str)
    frac = payload["fractional_count_fp"]
    post = payload["sign_test_post_mm_program"]
    eras = payload["by_mm_era"]
    common = payload["common_subset"]
    full = payload["full_sample"]["settlement_retention"]["headline_settlement_retention"]
    lines = [
        "C1-M2 part 2: three-way era, common-subset decomposition, horizon grid",
        f"prereg_id: {payload['prereg_id']}",
        f"prereg_hash: {payload['prereg_hash']}",
        "Unit: cents per contract. Both weightings on every headline.",
        "x1_go_no_go: FILL_IN",
        "EDT midnight-to-1 AM carried from Run A: 52",
        "",
        "Fractional count_fp",
        f"  handling: {frac['handling']}",
        f"  n_included: {frac['n_included']}",
        f"  first_climate_day: {frac['first_climate_day']}",
        f"  last_climate_day: {frac['last_climate_day']}",
        f"  premium_share_fractional: {frac['premium_share_fractional']}",
        f"  n_unattributed: {frac['n_unattributed']}",
        f"  post_program_unattributed_premium_share: "
        f"{frac['post_program_unattributed_premium_share']}",
        f"  source: {frac['source']}",
        "",
        "Full-sample settlement (including fractional prints; different universe "
        "from the common subset)",
        f"  trade-weighted: {_fmt_est(full['trade_weighted'])}",
        f"  day-weighted:   {_fmt_est(full['day_weighted'])}",
        "",
        "Three-way era. Sample size before the interval. JJA is the comparison.",
    ]
    for era_name in ("pre_mm_program", "mm_program", "post_mm_program"):
        block = eras[era_name]
        lines.append(f"{era_name}  program_status={block['program_status']}")
        lines.append(
            f"  n_fills={block['n_fills']}  n_days={block['n_days']}  "
            f"sum_count={block['sum_count']}"
        )
        lines.append(
            f"  all-seasons trade: {_fmt_est(block['all_seasons']['trade_weighted'])}"
        )
        lines.append(
            f"  all-seasons day:   {_fmt_est(block['all_seasons']['day_weighted'])}"
        )
        lines.append(
            f"  JJA n_fills={block['jja']['n_fills']} n_days={block['jja']['n_days']}"
        )
        lines.append(f"  JJA trade: {_fmt_est(block['jja']['trade_weighted'])}")
        lines.append(f"  JJA day:   {_fmt_est(block['jja']['day_weighted'])}")
        lines.append(f"  sign: {block['sign']}")
        if "cautions" in block:
            for caution in block["cautions"]:
                lines.append(f"  caution: {caution}")
        lines.append("")
    lines.extend(
        [
            "Sign test (post_mm_program)",
            f"  n_fills={post['n_fills']} n_days={post['n_days']} "
            f"sum_count={post['sum_count']}",
            f"  sign: {post['sign']}",
            f"  root_chat: {post['root_chat']}",
            "",
            "Common subset (effective, 30m, settlement all present)",
            f"  n_fills={common['n_fills']} n_days={common['n_days']}",
            f"  effective trade: {_fmt_est(common['effective_half_spread']['trade_weighted'])}",
            f"  effective day:   {_fmt_est(common['effective_half_spread']['day_weighted'])}",
            f"  30m trade: {_fmt_est(common['realised_30m']['trade_weighted'])}",
            f"  30m day:   {_fmt_est(common['realised_30m']['day_weighted'])}",
            f"  settlement trade: {_fmt_est(common['settlement_retention']['trade_weighted'])}",
            f"  settlement day:   {_fmt_est(common['settlement_retention']['day_weighted'])}",
            "  price_impact effective minus 30m",
            f"    trade: {_fmt_est(common['price_impact_effective_minus_30m']['trade_weighted'])}",
            f"    day:   {_fmt_est(common['price_impact_effective_minus_30m']['day_weighted'])}",
            "  price_impact 30m minus settlement",
            f"    trade: {_fmt_est(common['price_impact_30m_minus_settlement']['trade_weighted'])}",
            f"    day:   {_fmt_est(common['price_impact_30m_minus_settlement']['day_weighted'])}",
            "",
            "Horizon x volume-decile grid is in the JSON (common subset; days ranked "
            "on full-universe daily premium).",
            "",
            "Maker concentration",
            f"  {payload['maker_concentration']['status']}: "
            f"{payload['maker_concentration']['reason']}",
            "",
            "D5 tape-versus-candle stays outside this lane.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = run()
    (OUT / "c1_m2_era.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    text = render_text(payload)
    (OUT / "c1_m2_era.txt").write_text(text, encoding="utf-8")
    print(text[:6000])
    print("wrote", OUT / "c1_m2_era.json")
    print("wrote", OUT / "c1_m2_era.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
