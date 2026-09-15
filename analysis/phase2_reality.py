"""Phase 2 reality checks that are not the crossed-state gate or M/A.

2.2 coverage, 2.3 B1 settlement over labelled days, 2.4 label noise vs K2,
2.5 six-bracket enumeration, 2.8 fee curve at observed price deciles.

2.1 is ``analysis.crossed_rate``. 2.6 is ``analysis.trade_turnover``.
2.7 is the existing ledger tests; this script re-runs the byte-identity check.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from analysis.bracket_enumeration import build_daily_table
from analysis.build_labels import high_at_snapshot, read_clinyc, read_labels
from analysis.trade_turnover import load_trade_frame
from ingestion.climate_day import climate_dst_status, parse_lst_clock
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START
from wxmm.backtest.ledger import refuse_unless_preregistered
from wxmm.backtest.replay import MarketEvent, run
from wxmm.core.errors import ConfigNotPreregistered, LeakageError
from wxmm.core.types import (
    BookLevel,
    BookSnapshot,
    ClockBoundStore,
    FrozenClock,
    InMemoryAsOfStore,
    published_record,
)
from wxmm.settlement.eras import kalshi_rule_for, kalshi_snapshot_utc
from wxmm.strategy.view import MarketView, ProposedOrder
from wxmm.venues.base import get_venue
from wxmm.venues.kalshi.fees import FeeRounding, taker_fee

K2_LABEL_NOISE = {"numerator": 1, "denominator": 2416, "rate": 1 / 2416}
ERA_BOUNDARIES = (date(2021, 12, 25), date(2021, 12, 26), date(2024, 9, 3), date(2024, 9, 4))


def _settlement_census(
    observations: Sequence[Any],
    labelled_days: Sequence[date],
    clinyc_rows: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    classes: Counter[str] = Counter()
    era_flips: list[str] = []
    midnight_1am_edt = 0
    midnight_1am_examples: list[str] = []
    by_day_obs = {day: [o for o in observations if o.climate_day == day] for day in labelled_days}
    for day in labelled_days:
        high, n_later = high_at_snapshot(observations, day)
        same = by_day_obs.get(day, [])
        if not same:
            classes["no_cli"] += 1
            continue
        if high is None:
            classes["ambiguous"] += 1
            continue
        rule = kalshi_rule_for(day)
        snapshot = kalshi_snapshot_utc(day, rule, tuple(same))
        later = [
            o
            for o in same
            if o.is_full_day and o.available_at > snapshot and o.high_f is not None
        ]
        if later and any(o.high_f != high for o in later):
            classes["raising"] += 1
        else:
            classes["clean"] += 1
        if day in ERA_BOUNDARIES or (day + timedelta(days=1)) in ERA_BOUNDARIES:
            era_flips.append(day.isoformat())

    for row in clinyc_rows:
        climate = date.fromisoformat(str(row["climate_date"]))
        if climate not in set(labelled_days):
            continue
        parsed = parse_lst_clock(str(row.get("time_of_high_raw") or ""))
        if parsed is None:
            continue
        hour, _minute = parsed
        if hour != 0:
            continue
        if climate_dst_status(climate) != "edt":
            continue
        midnight_1am_edt += 1
        if len(midnight_1am_examples) < 20:
            midnight_1am_examples.append(
                f"{climate.isoformat()} time_of_high_raw={row.get('time_of_high_raw')!r}"
            )

    return {
        "n_labelled_days": len(labelled_days),
        "classes": dict(classes),
        "era_boundary_days_in_scope": era_flips,
        "n_max_midnight_to_1am_edt": midnight_1am_edt,
        "midnight_1am_edt_examples": midnight_1am_examples,
        "midnight_1am_path_exercised": midnight_1am_edt > 0,
        "note": (
            "If n_max_midnight_to_1am_edt is 0, the LST previous-day assignment "
            "for a max between midnight and 1 AM local during EDT has never been exercised."
        ),
    }


def _label_noise(venue_disagree: int, venue_compared: int) -> dict[str, Any]:
    ours = (venue_disagree / venue_compared) if venue_compared else None
    return {
        "this_run": {
            "n_disagree": venue_disagree,
            "n_compared": venue_compared,
            "rate": ours,
        },
        "k2": K2_LABEL_NOISE,
        "agree": ours == K2_LABEL_NOISE["rate"] if ours is not None else None,
        "note": "Report both if they disagree; do not tune to match.",
    }


def _six_bracket(markets: Sequence[Mapping[str, Any]], start: date) -> dict[str, Any]:
    daily = build_daily_table(list(markets), start_date=start)
    if daily.empty:
        return {"n_days": 0, "n_not_six": None, "days_bracket_count_ne_6": []}
    not_six = daily[daily["n_brackets"] != 6]
    return {
        "n_days": int(len(daily)),
        "n_not_six": int(len(not_six)),
        "days_bracket_count_ne_6": not_six["climate_date"].astype(str).tolist()[:200],
        "n_brackets_value_counts": daily["n_brackets"].value_counts().to_dict(),
        "hardcoded_width": False,
    }


def _fee_curve(prices: Sequence[float]) -> dict[str, Any]:
    rows = []
    for contracts in (1, 100, 500):
        for price in prices:
            p = Decimal(str(price))
            cont = taker_fee(contracts, p, FeeRounding.CONTINUOUS)
            cent = taker_fee(contracts, p, FeeRounding.PER_ORDER_CENT)
            rows.append(
                {
                    "contracts": contracts,
                    "price": str(p),
                    "continuous": str(cont.amount),
                    "per_order_cent": str(cent.amount),
                    "formula": "round_up(0.07*C*P*(1-P))",
                }
            )
    return {"n_rows": len(rows), "rows": rows}


class _IdleStrategy:
    def on_snapshot(self, view: MarketView) -> list[ProposedOrder]:
        _ = view.books
        return []


def _ledger_book(ts: datetime) -> BookSnapshot:
    return BookSnapshot(
        market_id="KXHIGHNY-26JUL04-T90",
        valid_at=ts,
        available_at=ts,
        bids=(BookLevel(40, 5),),
        asks=(BookLevel(42, 5),),
        volume=0,
        ask_size_known=True,
        reconstructed=True,
        staleness=timedelta(seconds=15),
        two_sided=True,
    )


def _ledger_checks(prereg_dir: Path) -> dict[str, Any]:
    import yaml

    smoke = yaml.safe_load((prereg_dir / "stage_b1_smoke.yaml").read_text())
    refuse_unless_preregistered(smoke, prereg_dir)
    unregistered_ok = False
    try:
        refuse_unless_preregistered({"prereg_id": "not-a-real-config", "seed": 0}, prereg_dir)
    except ConfigNotPreregistered:
        unregistered_ok = True
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=timezone.utc)
    events = (
        MarketEvent(
            ts=ts,
            kind="book",
            market_id="KXHIGHNY-26JUL04-T90",
            payload=_ledger_book(ts),
            climate_day="2026-07-04",
        ),
    )
    venue = get_venue("kalshi", InMemoryAsOfStore())
    a = run(
        config=smoke,
        prereg_dir=prereg_dir,
        strategy=_IdleStrategy(),
        venue=venue,
        events=events,
    )
    b = run(
        config=smoke,
        prereg_dir=prereg_dir,
        strategy=_IdleStrategy(),
        venue=venue,
        events=events,
    )
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    clock = FrozenClock(ts)
    store = ClockBoundStore(InMemoryAsOfStore(), clock)
    rec = published_record(
        key="k",
        payload={"x": 1},
        valid_at=ts,
        source_published_at=future,
        source="test",
        ingest_run_id="phase2",
    )
    store.put(rec)
    future_raised = False
    try:
        store.get_record(rec, as_of=ts)
    except LeakageError:
        future_raised = True
    return {
        "unregistered_config_refused": unregistered_ok,
        "available_at_after_clock_now_raises": future_raised,
        "byte_identical_same_config_snapshot_seed": a.canonical_bytes() == b.canonical_bytes(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--labels", type=Path, default=Path("data/labels/settlement.json"))
    parser.add_argument("--clinyc", type=Path, default=Path("data/labels/clinyc.csv"))
    parser.add_argument(
        "--markets-json",
        type=Path,
        default=Path("data/trades/KXHIGHNY/markets.json"),
    )
    parser.add_argument("--start", type=date.fromisoformat, default=SIX_BRACKET_ERA_START)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--prereg-dir", type=Path, default=Path("prereg"))
    parser.add_argument("--out", type=Path, default=Path("analysis/out/phase2_reality.json"))
    parser.add_argument("--label-report", type=Path, default=Path("data/labels/report.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    labels = read_labels(args.labels) if args.labels.exists() else {}
    labelled_days = sorted({lab.climate_day for lab in labels.values()})
    observations = read_clinyc(args.clinyc) if args.clinyc.exists() else []
    clinyc_rows: list[dict[str, str]] = []
    if args.clinyc.exists():
        with args.clinyc.open(encoding="utf-8", newline="") as handle:
            clinyc_rows = list(csv.DictReader(handle))

    crossed_path = Path("analysis/out/crossed_rate.json")
    if crossed_path.exists():
        crossed_raw = json.loads(crossed_path.read_text(encoding="utf-8"))
        crossed_payload = {
            "rate": crossed_raw["as_specified"]["rate"],
            "verdict": crossed_raw["verdict"],
            "mean_gap_cents": crossed_raw["as_specified"]["mean_gap_cents"],
            "mean_uncrossed_gap_cents": crossed_raw["as_specified"][
                "mean_uncrossed_gap_cents"
            ],
        }
        n_trades = int(crossed_raw["n_trades"])
    else:
        crossed_payload = {"status": "NOT_RUN", "reason": "run analysis.crossed_rate first"}
        n_trades = None

    cov_path = Path("analysis/out/trade_anchor_coverage.json")
    if cov_path.exists():
        coverage = json.loads(cov_path.read_text(encoding="utf-8"))
    else:
        coverage = {"status": "NOT_RUN", "reason": "run analysis.v0_coverage first"}

    settlement = _settlement_census(observations, labelled_days, clinyc_rows)

    venue_compared = 0
    venue_disagree = 0
    if args.label_report is not None and args.label_report.exists():
        label_meta = json.loads(args.label_report.read_text(encoding="utf-8"))
        venue_compared = int(label_meta.get("n_venue_compared") or 0)
        venue_disagree = int(label_meta.get("n_venue_disagree") or 0)
    noise = _label_noise(venue_disagree, venue_compared)

    markets: list[dict[str, Any]] = []
    if args.markets_json.exists():
        loaded = json.loads(args.markets_json.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            markets = [m for m in loaded if isinstance(m, dict)]
    brackets = _six_bracket(markets, args.start)

    frame = load_trade_frame(args.parquet)
    import polars as pl

    yes = frame.get_column("yes_price").cast(pl.Float64)
    deciles = [float(yes.quantile(q) or 0.0) for q in (i / 10 for i in range(1, 10))]
    fees = _fee_curve(deciles)
    ledger = _ledger_checks(args.prereg_dir)

    payload = {
        "n_trades": n_trades,
        "crossed": crossed_payload,
        "coverage": coverage,
        "settlement": settlement,
        "label_noise": noise,
        "six_bracket": brackets,
        "fees": fees,
        "ledger": ledger,
        "s2_complete_books_reference": 0.224,
        "note": "S2 22.4% is complete books, a different quantity than trade-anchor coverage.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
