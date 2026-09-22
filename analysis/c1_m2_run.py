"""C1-M2 realised half-spread on the KXHIGHNY tape.

Retention is cents per contract. Every headline is reported trade-weighted
and day-weighted together. X1 go_no_go is not filled.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from analysis.build_labels import read_clinyc, read_labels
from wxmm.analysis.maker_taker import season_of
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    RawTrade,
    ladder_regime_for,
    read_trades_parquet,
)
from wxmm.fairvalue.anchor_trades import yes_space_print
from wxmm.measure.c1_m2 import (
    MM_PROGRAM_START,
    MarkedFill,
    SettlementFill,
    TapePrint,
    both_weightings,
    mm_era_of,
    replay_marks,
    summarise_retention,
)
from wxmm.settlement.resolvers.kalshi import resolve as resolve_kalshi

OUT = Path("analysis/out/c1_m2")
TRADES = Path("data/trades/KXHIGHNY")
LABELS = Path("data/labels/settlement.json")
CLINYC = Path("data/labels/clinyc.csv")
HORIZON = timedelta(minutes=30)
AS_OF = datetime(2026, 9, 22, tzinfo=timezone.utc)


def _float_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _replay_ticker(trades: list[RawTrade]) -> tuple[MarkedFill, ...]:
    prints = [
        TapePrint(
            ts=trade.created_time,
            yes_price=yes_space_print(trade).yes_price,
            direction=yes_space_print(trade).direction,
            count=trade.count,
            trade_id=trade.trade_id,
        )
        for trade in trades
        if not trade.is_block_trade and Decimal(int(trade.count)) == trade.count
    ]
    return replay_marks(prints, horizon=HORIZON)


def _revision_audit(clinyc: Path, labelled_days: set[date]) -> dict[str, Any]:
    observations = read_clinyc(clinyc)
    raising: list[dict[str, Any]] = []
    high_changed: list[str] = []
    for day in sorted(labelled_days):
        result = resolve_kalshi(day, observations, AS_OF)
        notice = result.revision
        if notice is None or notice.n_later <= 0:
            continue
        raising.append(
            {
                "climate_day": day.isoformat(),
                "high_f": result.high_f,
                "n_later": notice.n_later,
                "later_highs": list(notice.later_highs),
                "high_changed": notice.high_changed,
                "ignored_for_resolution": notice.ignored_for_resolution,
            }
        )
        if notice.high_changed:
            high_changed.append(day.isoformat())
    return {
        "definition": (
            "n_later > 0 on a labelled day: full-day CLINYC after the era "
            "snapshot and at or before as_of. high_f is the snapshot value."
        ),
        "n_raising": len(raising),
        "raising_days": raising,
        "n_high_changed": len(high_changed),
        "high_changed_days": high_changed,
    }


def run() -> dict[str, Any]:
    if not TRADES.is_dir() or not any(TRADES.glob("*.parquet")):
        return {
            "status": "NOT_RUN",
            "reason": f"no trade parquet under {TRADES}",
        }
    if not LABELS.exists():
        return {"status": "NOT_RUN", "reason": f"no labels at {LABELS}"}

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
    n_effective = 0
    fractional_premium = 0.0
    integer_premium = 0.0
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
            if Decimal(int(trade.count)) != trade.count:
                n_fractional += 1
                fractional_premium += premium
                continue
            integer_premium += premium
            by_ticker[trade.ticker].append(trade)

        for ticker, rows in by_ticker.items():
            label = labels[ticker]
            marked = {row.trade_id: row for row in _replay_ticker(rows)}
            payoff = Decimal("1") if label.yes_won else Decimal("0")
            for row in rows:
                fill = marked[row.trade_id]
                retention = fill.realised_cents(payoff)
                if retention is None:
                    continue
                n_primary += 1
                record = SettlementFill(
                    climate_day=label.climate_day,
                    season=season_of(label.climate_day),
                    mm_era=mm_era_of(label.climate_day),
                    retention_cents=float(retention),
                    effective_cents=_float_or_none(fill.effective_cents()),
                    premium=float(row.yes_price * row.count),
                )
                fills.append(record)
                if record.effective_cents is not None:
                    n_effective += 1
                    effective_by_day[record.climate_day].append(record.effective_cents)
                h30 = fill.realised_cents(fill.mark_strict_mid)
                if h30 is not None:
                    h30_by_day[record.climate_day].append(float(h30))
        del by_ticker

    summary = summarise_retention(fills, seed=0, n_resample=1000)

    return {
        "status": "OK",
        "unit": "cents_per_contract",
        "uncrossed_definition": "ask > bid; ties excluded from the strict touch mid",
        "mm_program_start": MM_PROGRAM_START.isoformat(),
        "mm_program_source": (
            "CFTC Kalshi Market Maker Program terms, effective 2024-03-11 "
            "(rules02262412176). Designated-contract scope is not verified "
            "for every KXHIGHNY bracket."
        ),
        "label_noise_definition": (
            "venue disagreement, data-sources.md §1.3.1; not recomputed here"
        ),
        "edt_midnight_1am_carried_from_run_A": 52,
        "x1_go_no_go": "FILL_IN",
        "n_primary_fills": n_primary,
        "n_block_excluded": n_block,
        "n_unlabelled_excluded": n_unlabelled,
        "n_strict_pre_mid": n_effective,
        "fractional_count_fp": {
            "n_skipped": n_fractional,
            "premium_skipped": fractional_premium,
            "premium_kept": integer_premium,
            "premium_share_skipped": (
                fractional_premium / (fractional_premium + integer_premium)
                if (fractional_premium + integer_premium)
                else None
            ),
            "note": "Non-integer count_fp excluded from retention. Counted, not floored.",
        },
        "revision_detection": revisions,
        "settlement_retention": summary,
        "effective_half_spread_strict_mid": {
            key: {
                "point_cents_per_contract": est.point,
                "ci_low_cents_per_contract": est.ci_low,
                "ci_high_cents_per_contract": est.ci_high,
                "n_fills": est.n,
                "n_days": est.n_days,
            }
            for key, est in both_weightings(effective_by_day, seed=7).items()
        },
        "realised_30m_strict_mid": {
            key: {
                "point_cents_per_contract": est.point,
                "ci_low_cents_per_contract": est.ci_low,
                "ci_high_cents_per_contract": est.ci_high,
                "n_fills": est.n,
                "n_days": est.n_days,
            }
            for key, est in both_weightings(h30_by_day, seed=8).items()
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = run()
    (OUT / "c1_m2.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    text = json.dumps(
        {k: payload[k] for k in payload if k != "settlement_retention"},
        indent=2,
        default=str,
    )
    print(text[:4000])
    if payload.get("status") == "OK":
        headline = payload["settlement_retention"]["headline_settlement_retention"]
        print("HEADLINE", json.dumps(headline, indent=2))
        print("GATE0", json.dumps(payload["settlement_retention"]["gate0"], indent=2))
    print("wrote", OUT / "c1_m2.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
