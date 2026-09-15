"""Thin CLI for C1-X1 on the pulled corpus. Does not invent go/no-go numbers.

Prereg go_no_go is still FILL_IN. This runner measures maker/taker returns
and writes them; it does not fill the yaml.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from analysis.build_labels import read_labels
from analysis.trade_turnover import load_trade_frame, m_and_a
from wxmm.analysis.maker_taker import (
    clustered_bootstrap_mean_ci,
    season_of,
    select_primary,
    x1a_report,
)
from wxmm.analysis.population import bracket_day_nets, x1b_report
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START, read_trades_parquet
from wxmm.eval.flb import x1c_report


def _ci_excludes_zero(ci: tuple[Decimal, Decimal] | None) -> bool | None:
    if ci is None:
        return None
    return not (ci[0] <= Decimal("0") <= ci[1])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--labels", type=Path, default=Path("data/labels/settlement.json"))
    parser.add_argument("--prereg", type=Path, default=Path("prereg/c1-x1-v1.yaml"))
    parser.add_argument("--start", type=date.fromisoformat, default=SIX_BRACKET_ERA_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--n-resample", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("analysis/out/c1_x1.json"))
    parser.add_argument(
        "--turnover",
        type=Path,
        default=Path("analysis/out/trade_turnover.json"),
        help="M and A from phase 2.6, used for the magnitude gate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prereg: dict[str, Any] = yaml.safe_load(args.prereg.read_text(encoding="utf-8"))
    labels = read_labels(args.labels)
    trades = read_trades_parquet(args.parquet, strict_complement=False)
    if args.end is not None:
        trades = [t for t in trades if args.start <= t.climate_day <= args.end]
        labels = {k: v for k, v in labels.items() if args.start <= v.climate_day <= args.end}
    else:
        trades = [t for t in trades if t.climate_day >= args.start]
        labels = {k: v for k, v in labels.items() if v.climate_day >= args.start}

    seed = int(prereg.get("seed", 0))
    primary, blocks, n_unlabelled = select_primary(trades, labels)
    x1a = x1a_report(
        primary,
        blocks,
        n_unlabelled=n_unlabelled,
        seed=seed,
        n_resample=args.n_resample,
    )
    x1b = x1b_report(primary)
    x1c = x1c_report(primary)

    jja = [row for row in primary if season_of(row.climate_day) == "JJA"]
    jja_groups = {
        day: [r.maker.net_return for r in jja if r.climate_day == day]
        for day in {r.climate_day for r in jja}
    }
    jja_maker = clustered_bootstrap_mean_ci(jja_groups, seed=seed, n_resample=args.n_resample)

    nets = bracket_day_nets(primary)
    flat = [net for net in nets if net.abs_net_over_gross < Decimal("0.10")]
    flat_keys = {(net.ticker, net.climate_day) for net in flat}
    flat_trades = [row for row in primary if (row.ticker, row.climate_day) in flat_keys]
    flat_groups = {
        day: [r.maker.net_return for r in flat_trades if r.climate_day == day]
        for day in {r.climate_day for r in flat_trades}
    }
    flat_ci = clustered_bootstrap_mean_ci(flat_groups, seed=seed, n_resample=args.n_resample)
    near_flat = next((row for row in x1b.thresholds if row.threshold == Decimal("0.10")), None)

    turnover = None
    if args.turnover.exists():
        turnover = json.loads(args.turnover.read_text(encoding="utf-8"))
    elif args.parquet.exists():
        turnover = m_and_a(load_trade_frame(args.parquet))

    payload = {
        "prereg_id": prereg.get("prereg_id"),
        "go_no_go_still_fill_in": True,
        "n_primary": x1a.n_primary_trades,
        "n_unlabelled": n_unlabelled,
        "x1a": x1a.model_dump(mode="json"),
        "x1b": x1b.model_dump(mode="json"),
        "x1c": x1c.model_dump(mode="json"),
        "sign_gate": {
            "maker_mean_net": str(x1a.maker_mean_net),
            "maker_ci_net": [str(x) for x in x1a.maker_ci_net] if x1a.maker_ci_net else None,
            "ci_excludes_zero": _ci_excludes_zero(x1a.maker_ci_net),
            "near_flat_n_bracket_days": len(flat),
            "near_flat_maker_ci": [str(x) for x in flat_ci] if flat_ci else None,
            "near_flat_ci_excludes_zero": _ci_excludes_zero(flat_ci),
            "weather_maker_fee_usd": "0.00",
            "gross_equals_net_for_maker": True,
        },
        "jja": {
            "n_trades": len(jja),
            "maker_clustered_ci": [str(x) for x in jja_maker] if jja_maker else None,
            "ci_excludes_zero": _ci_excludes_zero(jja_maker),
            "headline": "JJA",
        },
        "near_flat_0_10": near_flat.model_dump(mode="json") if near_flat is not None else None,
        "magnitude_from_M_and_A": turnover,
        "is_strategy_pnl": False,
        "note": "Measured; prereg go_no_go left FILL_IN. Do not invent those numbers.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    summary_keys = {k: v for k, v in payload.items() if k not in {"x1a", "x1b", "x1c"}}
    print(json.dumps(summary_keys, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
