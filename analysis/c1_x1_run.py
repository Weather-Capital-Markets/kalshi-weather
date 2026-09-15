"""Thin CLI for C1-X1 on the pulled corpus. Does not invent go/no-go numbers.

Prereg go_no_go is still FILL_IN. This runner measures maker/taker returns
and writes them; it does not fill the yaml.

Monthly shards are attributed one file at a time so the 3.4M-print corpus fits.
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
    CompactFill,
    clustered_bootstrap_mean_ci,
    select_primary_compact,
    x1a_report_compact,
)
from wxmm.analysis.population import (
    bracket_day_nets_compact,
    x1b_from_nets,
)
from wxmm.analysis.trades_ingest import (
    SIX_BRACKET_ERA_START,
    parquet_shard_paths,
    read_trades_parquet,
)
from wxmm.eval.flb import x1c_report_compact


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


def _load_compact(
    parquet: Path,
    labels: dict[str, Any],
    *,
    start: date,
    end: date | None,
) -> tuple[list[CompactFill], list[CompactFill], int, int]:
    scoped = {
        ticker: lab
        for ticker, lab in labels.items()
        if lab.climate_day >= start and (end is None or lab.climate_day <= end)
    }
    primary: list[CompactFill] = []
    blocks: list[CompactFill] = []
    n_unlabelled = 0
    n_skipped = 0
    for shard in parquet_shard_paths(parquet):
        trades = read_trades_parquet(shard, strict_complement=False)
        trades = [
            t
            for t in trades
            if t.climate_day >= start and (end is None or t.climate_day <= end)
        ]
        if not trades:
            continue
        part_p, part_b, part_u, part_s = select_primary_compact(
            trades, scoped, era_start=start
        )
        primary.extend(part_p)
        blocks.extend(part_b)
        n_unlabelled += part_u
        n_skipped += part_s
        del trades, part_p, part_b
        print(
            f"  attributed {shard.name} primary={len(primary)} "
            f"unlabelled={n_unlabelled} skipped={n_skipped}",
            flush=True,
        )
    return primary, blocks, n_unlabelled, n_skipped


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prereg: dict[str, Any] = yaml.safe_load(args.prereg.read_text(encoding="utf-8"))
    labels = read_labels(args.labels)
    primary, blocks, n_unlabelled, n_skipped = _load_compact(
        args.parquet,
        labels,
        start=args.start,
        end=args.end,
    )

    seed = int(prereg.get("seed", 0))
    x1a = x1a_report_compact(
        primary,
        blocks,
        n_unlabelled=n_unlabelled,
        seed=seed,
        n_resample=args.n_resample,
    )
    nets = bracket_day_nets_compact(primary)
    x1b = x1b_from_nets(nets)
    x1c = x1c_report_compact(primary)

    jja = [row for row in primary if row.season == "JJA"]
    jja_groups: dict[date, list[Decimal]] = {}
    for row in jja:
        jja_groups.setdefault(row.climate_day, []).append(Decimal(str(row.maker_net)))
    jja_maker = clustered_bootstrap_mean_ci(
        jja_groups, seed=seed, n_resample=args.n_resample
    )

    flat = [net for net in nets if net.abs_net_over_gross < Decimal("0.10")]
    flat_keys = {(net.ticker, net.climate_day) for net in flat}
    flat_groups: dict[date, list[Decimal]] = {}
    for row in primary:
        if (row.ticker, row.climate_day) in flat_keys:
            flat_groups.setdefault(row.climate_day, []).append(Decimal(str(row.maker_net)))
    flat_ci = clustered_bootstrap_mean_ci(
        flat_groups, seed=seed, n_resample=args.n_resample
    )
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
        "n_skipped_attribute_errors": n_skipped,
        "x1a": x1a.model_dump(mode="json"),
        "x1b": x1b.model_dump(mode="json"),
        "x1c": x1c.model_dump(mode="json"),
        "sign_gate": {
            "maker_mean_net": str(x1a.maker_mean_net),
            "maker_mean_gross": str(x1a.maker_mean_gross),
            "maker_ci_net": [str(x) for x in x1a.maker_ci_net] if x1a.maker_ci_net else None,
            "ci_excludes_zero": _ci_excludes_zero(x1a.maker_ci_net),
            "near_flat_n_bracket_days": len(flat),
            "near_flat_maker_ci": [str(x) for x in flat_ci] if flat_ci else None,
            "near_flat_ci_excludes_zero": _ci_excludes_zero(flat_ci),
            "weather_maker_fee_usd": "0.00",
            "gross_equals_net_for_maker": x1a.maker_mean_gross == x1a.maker_mean_net,
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
