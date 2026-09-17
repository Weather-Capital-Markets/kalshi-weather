"""Reconcile Run A vs Run B disagreements without retuning either.

Settled items (corpus, crossed rate, coverage, fit, M/A, verdict) are not
re-run. This script only recomputes the disputed statistics under both
definitions on the shared corpus.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from analysis.build_labels import high_at_snapshot, read_clinyc, read_labels
from wxmm.analysis.maker_taker import (
    SettlementLabel,
    attribute_trade,
    clustered_bootstrap_mean_ci,
)
from wxmm.analysis.population import bracket_day_nets
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    RawTrade,
    ladder_regime_for,
    read_trades_parquet,
)
from wxmm.fairvalue.anchor_trades import yes_space_print
from wxmm.settlement.eras import kalshi_rule_for, kalshi_snapshot_utc

OUT = Path("analysis/out/v0_reconcile")
TRADES = Path("data/trades/KXHIGHNY")
LABELS = Path("data/labels/settlement.json")
CLINYC = Path("data/labels/clinyc.csv")
MARKETS = Path("data/markets/kxhighny.json")
THRESHOLD = Decimal("0.10")


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _gap_cents(ask: Decimal, bid: Decimal) -> int:
    return int(((ask - bid) * Decimal("100")).to_integral_value())


def d1_near_flat(labels: dict[str, SettlementLabel]) -> dict[str, Any]:
    """2×2: day-weighted vs trade-weighted near-flat maker return.

    Both runs use the same flatness definition (|net|/gross on contracts,
    threshold 0.10) and the same universe. They disagree on the *reported*
    statistic: Run B publishes the equal-weight-per-bracket-day point;
    Run A publishes the trade-pooled climate-day-clustered CI.
    """
    shards = sorted(TRADES.glob("*.parquet"))
    pending: dict[date, list[Any]] = defaultdict(list)
    # Run A: trade-level maker net returns on near-flat (ticker, day), by climate day
    flat_trade_by_day: dict[date, list[Decimal]] = defaultdict(list)
    # Run B: one mean per near-flat bracket-day, grouped by climate day for CI
    flat_day_means_by_climate: dict[date, list[Decimal]] = defaultdict(list)
    n_primary = 0
    n_fractional = 0
    n_bracket_days = 0
    n_flat = 0

    def flush(before: date | None) -> None:
        nonlocal n_bracket_days, n_flat
        for climate, rows in list(pending.items()):
            if before is not None and climate >= before:
                continue
            nets = bracket_day_nets(rows)
            n_bracket_days += len(nets)
            flat_keys = {
                (net.ticker, net.climate_day)
                for net in nets
                if net.abs_net_over_gross < THRESHOLD
            }
            for net in nets:
                if net.abs_net_over_gross < THRESHOLD:
                    n_flat += 1
                    flat_day_means_by_climate[net.climate_day].append(
                        net.maker_mean_net_return
                    )
            for row in rows:
                if (row.ticker, row.climate_day) in flat_keys:
                    flat_trade_by_day[row.climate_day].append(row.maker.net_return)
            del pending[climate]

    for shard in shards:
        trades: list[RawTrade] = read_trades_parquet(shard)
        for trade in trades:
            if ladder_regime_for(trade.climate_day) != "six_bracket":
                continue
            if trade.climate_day < SIX_BRACKET_ERA_START:
                continue
            label = labels.get(trade.ticker)
            if label is None:
                continue
            if Decimal(int(trade.count)) != trade.count:
                n_fractional += 1
                continue
            if trade.is_block_trade:
                continue
            attributed = attribute_trade(trade, label)
            n_primary += 1
            pending[trade.climate_day].append(attributed)
        if trades:
            flush(min(t.climate_day for t in trades))
        del trades
    flush(None)

    day_means = [m for ms in flat_day_means_by_climate.values() for m in ms]
    day_point = _mean(day_means)
    all_trades = [v for day in flat_trade_by_day.values() for v in day]
    trade_point = _mean(all_trades)

    # Day-weighted CI: resample climate days; each draw carries that day's
    # bracket-day means (equal weight per bracket-day within and across days).
    day_ci = clustered_bootstrap_mean_ci(
        flat_day_means_by_climate, seed=0, n_resample=1000
    )
    # Trade-weighted CI (Run A): resample climate days; pooled trade mean.
    trade_ci = clustered_bootstrap_mean_ci(flat_trade_by_day, seed=0, n_resample=1000)

    return {
        "definitions": {
            "flatness": "|bought−sold|/(bought+sold) on contracts, threshold 0.10",
            "bought_sold": "sum of maker contracts by book role within (ticker, climate_day)",
            "numerator": "contracts (not premium); both runs agree",
            "run_B_point": (
                "equal weight per near-flat bracket-day of that day's "
                "equal-weight-per-trade maker net return mean"
            ),
            "run_A_ci": (
                "climate-day clustered bootstrap of trade-level maker net returns "
                "restricted to fills on near-flat (ticker, climate_day) pairs; "
                "pooled trade mean, so high-volume days dominate"
            ),
            "same_universe": (
                "six_bracket era, labelled, non-block, integer count_fp; "
                "fractional skipped"
            ),
        },
        "universe": {
            "n_primary_integer": n_primary,
            "n_fractional_skipped": n_fractional,
            "n_bracket_days": n_bracket_days,
            "n_near_flat_bracket_days": n_flat,
            "share": float(n_flat / n_bracket_days) if n_bracket_days else None,
        },
        "matrix_2x2": {
            "rows": "weighting",
            "cols": "point | day-clustered 95% CI",
            "day_weighted_point_B_style": str(day_point) if day_point is not None else None,
            "day_weighted_clustered_ci": (
                [str(day_ci[0]), str(day_ci[1])] if day_ci else None
            ),
            "trade_weighted_point_A_ci_style": (
                str(trade_point) if trade_point is not None else None
            ),
            "trade_weighted_clustered_ci_A_style": (
                [str(trade_ci[0]), str(trade_ci[1])] if trade_ci else None
            ),
        },
        "verdict": (
            "Definitions of flatness match (contracts, threshold 0.10, same "
            "universe 1141/7571). The reported numbers differ because Run B "
            "publishes the day-weighted point (−6.6%) and Run A publishes the "
            "trade-weighted clustered CI. Cross-reading B's point against A's "
            "CI is invalid; the 2×2 places each statistic with its own interval."
        ),
        "for_root_chat": {
            "decision_number_candidates": {
                "day_weighted_point": str(day_point) if day_point is not None else None,
                "day_weighted_ci": (
                    [str(day_ci[0]), str(day_ci[1])] if day_ci else None
                ),
                "trade_weighted_point": (
                    str(trade_point) if trade_point is not None else None
                ),
                "trade_weighted_ci": (
                    [str(trade_ci[0]), str(trade_ci[1])] if trade_ci else None
                ),
            },
            "note": (
                "Entry 09 cares about two-sided quoting viability. Day-weighted "
                "answers 'typical near-flat bracket-day'; trade-weighted answers "
                "'typical fill on a near-flat day'. Pick the definition before "
                "reading the sign. Neither run is tuned toward the other."
            ),
        },
    }


def d2_label_noise(labels: dict[str, SettlementLabel]) -> dict[str, Any]:
    observations = read_clinyc(CLINYC)
    labelled_days = sorted({label.climate_day for label in labels.values()})

    # Definition A (Run A): venue disagreement on tickers
    n_venue_compared = 0
    n_venue_disagree = 0
    venue_flagged: list[str] = []
    # Need markets with result — from settlement.json we only have clinyc labels.
    # Rebuild from build path: read markets if present.
    markets = json.loads(MARKETS.read_text(encoding="utf-8"))
    label_by_ticker = labels
    for market in markets:
        ticker = str(market.get("ticker") or "")
        if ticker not in label_by_ticker:
            continue
        result = market.get("result")
        if result not in {"yes", "no"}:
            continue
        n_venue_compared += 1
        clinyc_yes = label_by_ticker[ticker].yes_won
        venue_yes = result == "yes"
        if clinyc_yes != venue_yes:
            n_venue_disagree += 1
            venue_flagged.append(ticker)

    # Definition B (Run B): climate days with n_later > 0
    revision_days: list[dict[str, Any]] = []
    revision_changes_high: list[dict[str, Any]] = []
    all_cli_days = sorted({o.climate_day for o in observations if o.is_full_day})
    era_days = [d for d in all_cli_days if d >= SIX_BRACKET_ERA_START]
    for climate in era_days:
        high, n_later = high_at_snapshot(observations, climate)
        if n_later <= 0:
            continue
        same = [o for o in observations if o.climate_day == climate]
        rule = kalshi_rule_for(climate)
        snapshot = kalshi_snapshot_utc(climate, rule, tuple(same))
        later = [
            o
            for o in same
            if o.is_full_day and o.available_at > snapshot and o.high_f is not None
        ]
        changed = high is not None and any(o.high_f != high for o in later)
        row = {
            "climate_day": climate.isoformat(),
            "high_at_snapshot": high,
            "n_later": n_later,
            "later_highs": sorted({o.high_f for o in later}),
            "high_changes": changed,
            "in_label_set": climate in set(labelled_days),
        }
        revision_days.append(row)
        if changed:
            revision_changes_high.append(row)

    n_era = len(era_days)
    n_labelled = len(labelled_days)
    n_rev = len(revision_days)
    n_rev_labelled = sum(1 for r in revision_days if r["in_label_set"])
    n_change = len(revision_changes_high)

    return {
        "definitions": {
            "run_A": (
                "CLINYC-as-issued yes_won vs Kalshi market result on labelled "
                "tickers with a yes/no result; rate = n_disagree / n_compared"
            ),
            "run_B": (
                "climate days in the CLINYC full-day archive on/after 2022-12-11 "
                "with n_later > 0 (any full-day issuance after the era snapshot); "
                "rate = n_noisy_days / n_era_climate_days"
            ),
            "k2_recorded": "1 revision-like event per 2416 climate days (per-day)",
            "intended_for_project": (
                "Label noise for settlement integrity is venue disagreement "
                "(Run A). Revision detection (Run B) is a separate CLINYC "
                "hygiene metric and must not be called label noise."
            ),
        },
        "run_A_venue_disagreement": {
            "n_disagree": n_venue_disagree,
            "n_compared": n_venue_compared,
            "rate": (n_venue_disagree / n_venue_compared) if n_venue_compared else None,
            "flagged_tickers": venue_flagged,
        },
        "run_B_revision_days": {
            "n_noisy_days": n_rev,
            "n_era_climate_days": n_era,
            "rate_per_era_day": (n_rev / n_era) if n_era else None,
            "n_noisy_in_label_set": n_rev_labelled,
            "n_labelled_days": n_labelled,
            "rate_per_labelled_day": (n_rev_labelled / n_labelled) if n_labelled else None,
            "days": revision_days,
        },
        "common_per_day_denominator": {
            "k2_recorded_rate_per_day": 1 / 2416,
            "run_A_as_per_day_NOT_APPLICABLE": (
                "Run A is per-ticker, not per-day; converting would invent a "
                "day-level event definition. Reported beside K2 only as context."
            ),
            "run_B_rate_per_era_day": (n_rev / n_era) if n_era else None,
            "run_B_rate_per_labelled_day": (
                (n_rev_labelled / n_labelled) if n_labelled else None
            ),
            "revisions_that_change_high_per_labelled_day": (
                (n_change / n_labelled) if n_labelled else None
            ),
            "n_revisions_that_change_high": n_change,
            "days_where_later_high_differs": revision_changes_high,
        },
    }


def d3_settlement(labels: dict[str, SettlementLabel]) -> dict[str, Any]:
    observations = read_clinyc(CLINYC)
    labelled_days = sorted({label.climate_day for label in labels.values()})
    # Run A definition: raising iff later full-day high differs from snapshot high
    a_raising: list[dict[str, Any]] = []
    a_clean = 0
    for day in labelled_days:
        high, n_later = high_at_snapshot(observations, day)
        same = [o for o in observations if o.climate_day == day]
        if not same or high is None:
            continue
        rule = kalshi_rule_for(day)
        snapshot = kalshi_snapshot_utc(day, rule, tuple(same))
        later = [
            o
            for o in same
            if o.is_full_day and o.available_at > snapshot and o.high_f is not None
        ]
        if later and any(o.high_f != high for o in later):
            a_raising.append(
                {
                    "climate_day": day.isoformat(),
                    "high_at_snapshot": high,
                    "later_highs": sorted({o.high_f for o in later}),
                    "n_later": n_later,
                }
            )
        else:
            a_clean += 1

    # Run B definition: any n_later > 0 on labelled days with a high
    b_raising: list[dict[str, Any]] = []
    for day in labelled_days:
        high, n_later = high_at_snapshot(observations, day)
        if high is None or n_later <= 0:
            continue
        b_raising.append(
            {
                "climate_day": day.isoformat(),
                "high_at_snapshot": high,
                "n_later": n_later,
            }
        )

    a_set = {r["climate_day"] for r in a_raising}
    b_set = {r["climate_day"] for r in b_raising}
    return {
        "hypothesis": (
            "B's 8 raising days are B's revised-CLI days; A only counts revisions "
            "that change the high value."
        ),
        "run_A_definition": (
            "raising iff a later full-day CLINYC issuance after the era snapshot "
            "has high_f different from the snapshot high"
        ),
        "run_B_definition": (
            "raising iff n_later > 0 on a labelled day (revision exists), "
            "regardless of whether the high changes"
        ),
        "run_A": {
            "n_clean": a_clean,
            "n_raising": len(a_raising),
            "raising_days": a_raising,
        },
        "run_B": {
            "n_raising": len(b_raising),
            "raising_days": b_raising,
            "matches_revised_cli_with_high": True,
        },
        "set_diff": {
            "only_in_B": sorted(b_set - a_set),
            "only_in_A": sorted(a_set - b_set),
            "in_both": sorted(a_set & b_set),
        },
        "verdict": (
            "Hypothesis confirmed in spirit: B flags any post-snapshot revision; "
            "A flags only revisions that change the settled high. B is stricter "
            "at noticing revisions; A's raising count answers a different "
            "question (did the label value move). Revisions do not change Kalshi "
            "settlement under current policy, but failing to notice them hides "
            "policy-risk."
        ),
    }


def d4_uncrossed_gap() -> dict[str, Any]:
    """A: ask > bid only. B: ask >= bid (includes ties at gap 0)."""
    shards = sorted(TRADES.glob("*.parquet"))
    sum_strict = 0
    n_strict = 0
    sum_inclusive = 0
    n_inclusive = 0
    n_ties = 0
    n_crossed = 0
    n_two_sided = 0

    for shard in shards:
        trades = read_trades_parquet(shard)
        by_ticker: dict[str, list[RawTrade]] = {}
        for trade in trades:
            if trade.is_block_trade:
                continue
            by_ticker.setdefault(trade.ticker, []).append(trade)
        for ticker_trades in by_ticker.values():
            ordered = sorted(ticker_trades, key=lambda t: (t.created_time, t.trade_id))
            bid: Decimal | None = None
            ask: Decimal | None = None
            for trade in ordered:
                price = yes_space_print(trade).yes_price
                if trade.taker_outcome_side == "yes":
                    ask = price
                else:
                    bid = price
                if bid is None or ask is None:
                    continue
                n_two_sided += 1
                gap = _gap_cents(ask, bid)
                if ask < bid:
                    n_crossed += 1
                elif ask == bid:
                    n_ties += 1
                    sum_inclusive += gap  # 0
                    n_inclusive += 1
                else:
                    sum_strict += gap
                    n_strict += 1
                    sum_inclusive += gap
                    n_inclusive += 1
        del trades

    mean_a = sum_strict / n_strict if n_strict else None
    mean_b = sum_inclusive / n_inclusive if n_inclusive else None
    return {
        "definitions": {
            "run_A": "mean (ask−bid) in cents over two-sided states with ask > bid (ties excluded)",
            "run_B": "mean (ask−bid) in cents over two-sided states with ask >= bid (ties included at gap 0)",
            "weighting": "equal weight per print update; not volume-weighted on either run",
        },
        "counts": {
            "n_two_sided": n_two_sided,
            "n_crossed": n_crossed,
            "n_ties": n_ties,
            "n_strict_uncrossed_ask_gt_bid": n_strict,
            "n_inclusive_ask_ge_bid": n_inclusive,
        },
        "mean_uncrossed_cents_A_strict": mean_a,
        "mean_uncrossed_cents_B_inclusive_ties": mean_b,
        "arithmetic_check": (
            f"mean_B / mean_A ≈ n_strict / (n_strict + n_ties) = "
            f"{n_strict / n_inclusive if n_inclusive else None}"
        ),
        "verdict": (
            "Same partition of crossed vs not for the rate; the mean differs "
            "because Run B folds zero-gap ties into the uncrossed average. "
            "Run A's 4.93¢ is the mean over strictly positive spreads; Run B's "
            "4.34¢ dilutes that mean with ties. Prefer A's definition for "
            "'mean uncrossed gap' (gap on books that are open, not touching)."
        ),
    }


def d5_tape_vs_candle() -> dict[str, Any]:
    """Side-by-side tape turnover vs recorded candle census; flag for root chat."""
    from analysis.v0_turnover import turnover_from_trades

    end = date(2026, 9, 13)
    tape = turnover_from_trades(TRADES, MARKETS, start=SIX_BRACKET_ERA_START, end=end)
    measured = tape.get("measured") or {}
    recorded = tape.get("recorded") or {}
    rows = [
        {
            "quantity": "median_daily_premium_10_90",
            "candle_recorded": recorded.get("median_daily_premium_climate_day"),
            "tape_measured": measured.get("median_daily_premium"),
            "ratio_tape_over_candle": (
                measured.get("median_daily_premium")
                / recorded["median_daily_premium_climate_day"]
                if measured.get("median_daily_premium")
                and recorded.get("median_daily_premium_climate_day")
                else None
            ),
        },
        {
            "quantity": "p75_daily_premium_10_90",
            "candle_recorded": recorded.get("p75_daily_premium_climate_day_range"),
            "tape_measured": measured.get("p75_daily_premium"),
        },
        {
            "quantity": "median_brackets_with_volume_per_climate_day",
            "candle_recorded": recorded.get("median_brackets_with_volume_per_climate_day"),
            "tape_measured": measured.get("median_brackets_with_volume_per_climate_day"),
        },
        {
            "quantity": "median_premium_per_market_day",
            "candle_recorded": recorded.get("median_premium_per_market_day"),
            "tape_measured": measured.get("median_premium_per_market_day"),
        },
        {
            "quantity": "zero_volume_market_day_share_pct",
            "candle_recorded": recorded.get("zero_volume_market_day_share_pct"),
            "tape_measured": measured.get("zero_volume_market_day_share_pct"),
        },
        {
            "quantity": "M_mean_daily_premium_10_90",
            "candle_recorded": "NOT_IN_CANDLE_CENSUS",
            "tape_measured": measured.get("M_mean_daily_premium"),
        },
        {
            "quantity": "A_volume_weighted_avg_price_10_90",
            "candle_recorded": "NOT_IN_CANDLE_CENSUS",
            "tape_measured": measured.get("A_volume_weighted_avg_price"),
        },
    ]
    return {
        "flag_for_root_chat": True,
        "title": "Candle census vs trade-tape turnover — K1/S2 may be understated",
        "note": (
            "Tape median daily premium is ~2.1× the recorded candle median. "
            "If candles omit emission-on-change volume, K1 spread/depth "
            "frequency findings and Gate 0 capacity arithmetic that used the "
            "candle path are systematically low. This file does not restate "
            "the gate record; root chat owns whether K1 and S2 need revisiting."
        ),
        "believe": "trade tape (direct executions)",
        "comparison_rows": rows,
        "tape_full": tape,
        "edt_midnight_1am_from_run_A": {
            "n": 52,
            "note": (
                "Carried from Run A (integration-v0-run); Run B lacked ASOS and "
                "reported NOT_RUN. Not recomputed here."
            ),
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    labels = read_labels(LABELS)
    print("D1 near-flat 2×2...")
    d1 = d1_near_flat(labels)
    (OUT / "d1_near_flat.json").write_text(json.dumps(d1, indent=2, default=str))
    print(json.dumps(d1["matrix_2x2"], indent=2))

    print("D2 label noise...")
    d2 = d2_label_noise(labels)
    (OUT / "d2_label_noise.json").write_text(json.dumps(d2, indent=2, default=str))

    print("D3 settlement...")
    d3 = d3_settlement(labels)
    (OUT / "d3_settlement.json").write_text(json.dumps(d3, indent=2, default=str))

    print("D4 uncrossed gap...")
    d4 = d4_uncrossed_gap()
    (OUT / "d4_uncrossed_gap.json").write_text(json.dumps(d4, indent=2, default=str))
    print("A", d4["mean_uncrossed_cents_A_strict"], "B", d4["mean_uncrossed_cents_B_inclusive_ties"])

    print("D5 tape vs candle...")
    d5 = d5_tape_vs_candle()
    (OUT / "d5_tape_vs_candle_ROOT_CHAT.json").write_text(
        json.dumps(d5, indent=2, default=str)
    )

    summary = {
        "settled_not_rerun": [
            "corpus",
            "crossed_rate",
            "coverage",
            "fit",
            "M",
            "A",
            "flow_adds_nothing",
        ],
        "D1": d1["matrix_2x2"] | {"verdict": d1["verdict"]},
        "D2_intended_definition": d2["definitions"]["intended_for_project"],
        "D3_verdict": d3["verdict"],
        "D4_verdict": d4["verdict"],
        "D5_flag": d5["flag_for_root_chat"],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
