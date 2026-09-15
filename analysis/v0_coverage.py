"""Run the v0 coverage pre-check. Does not fit.

python -m analysis.v0_coverage --parquet data/trades/KXHIGHNY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wxmm.analysis.trades_ingest import parquet_shard_paths, read_trades_parquet
from wxmm.core.errors import CoverageFloorRefused
from wxmm.fairvalue.anchor_trades import ObservedMapping, assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import (
    CoverageSlice,
    TradeAnchorCoverage,
    refuse_unless_coverage,
    trade_anchor_coverage,
)


def _merge_slices(parts: list[tuple[CoverageSlice, ...]]) -> tuple[CoverageSlice, ...]:
    by_key: dict[str, list[CoverageSlice]] = {}
    for group in parts:
        for sl in group:
            by_key.setdefault(sl.key, []).append(sl)
    out: list[CoverageSlice] = []
    for key in sorted(by_key):
        items = by_key[key]
        n_grid = sum(s.n_grid for s in items)
        n_two = sum(s.n_two_sided_uncrossed for s in items)
        n_one = sum(s.n_one_sided for s in items)
        n_missing = sum(s.n_missing for s in items)
        n_crossed = sum(s.n_crossed_resolved for s in items)
        out.append(
            CoverageSlice(
                key=key,
                n_grid=n_grid,
                n_two_sided_uncrossed=n_two,
                n_one_sided=n_one,
                n_missing=n_missing,
                n_crossed_resolved=n_crossed,
                share=(n_two / n_grid) if n_grid else 0.0,
            )
        )
    return tuple(out)


def coverage_from_shards(
    parquet: Path, *, mapping: ObservedMapping, floor: float
) -> TradeAnchorCoverage:
    parts: list[TradeAnchorCoverage] = []
    for shard in parquet_shard_paths(parquet):
        trades = read_trades_parquet(shard, strict_complement=False)
        if not trades:
            continue
        parts.append(trade_anchor_coverage(trades, mapping=mapping, floor=floor))
        del trades
    if not parts:
        raise FileNotFoundError(f"no trades in {parquet}")
    n_grid = sum(p.n_grid for p in parts)
    n_two = sum(p.n_two_sided_uncrossed for p in parts)
    n_one = sum(p.n_one_sided for p in parts)
    n_missing = sum(p.n_missing for p in parts)
    n_crossed = sum(p.n_crossed_resolved for p in parts)
    bid = tuple(s for p in parts for s in p.bid_staleness_seconds)
    ask = tuple(s for p in parts for s in p.ask_staleness_seconds)
    return TradeAnchorCoverage(
        n_grid=n_grid,
        n_two_sided_uncrossed=n_two,
        n_one_sided=n_one,
        n_missing=n_missing,
        n_crossed_resolved=n_crossed,
        two_sided_share=(n_two / n_grid) if n_grid else 0.0,
        crossed_rate=(n_crossed / n_grid) if n_grid else 0.0,
        floor=floor,
        by_season=_merge_slices([p.by_season for p in parts]),
        by_hours_to_close=_merge_slices([p.by_hours_to_close for p in parts]),
        by_bracket_role=_merge_slices([p.by_bracket_role for p in parts]),
        bid_staleness_seconds=bid,
        ask_staleness_seconds=ask,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C1-M1 v0 trade-anchor coverage")
    parser.add_argument("--floor", type=float, default=0.15)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--out", type=Path, default=Path("analysis/out/trade_anchor_coverage.json"))
    parser.add_argument(
        "--trades-json",
        type=Path,
        default=None,
        help="unused; coverage reads parquet",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.parquet.exists():
        json.dump(
            {
                "status": "NOT_RUN",
                "reason": f"no trade corpus at {args.parquet}; run analysis.pull_corpus first",
                "floor": args.floor,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2
    mapping_path = Path("analysis/out/crossed_rate.json")
    if mapping_path.exists():
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping = ObservedMapping(
            counts={
                ("yes", "ask"): int(raw["cross_tab"].get("yesxask", 0)),
                ("yes", "bid"): int(raw["cross_tab"].get("yesxbid", 0)),
                ("no", "ask"): int(raw["cross_tab"].get("noxask", 0)),
                ("no", "bid"): int(raw["cross_tab"].get("noxbid", 0)),
            },
            outcome_to_book=dict(raw["outcome_to_book"]),
            n_non_block=sum(int(v) for v in raw["cross_tab"].values()),
            status="clean",
        )
    else:
        first = parquet_shard_paths(args.parquet)
        sample = read_trades_parquet(first[0], strict_complement=False) if first else []
        mapping = assert_outcome_bookside_mapping([t for t in sample if not t.is_block_trade])
    coverage = coverage_from_shards(args.parquet, mapping=mapping, floor=args.floor)
    payload = coverage.as_dict()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    try:
        refuse_unless_coverage(coverage)
    except CoverageFloorRefused as exc:
        print(f"coverage floor refused (report, do not halt Phase 2): {exc}", file=sys.stderr)
    return 0


# Re-export for tests / notebooks.
__all__ = [
    "assert_outcome_bookside_mapping",
    "refuse_unless_coverage",
    "trade_anchor_coverage",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
