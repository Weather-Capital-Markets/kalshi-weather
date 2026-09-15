"""Run the v0 coverage pre-check. Does not fit.

python -m analysis.v0_coverage --parquet data/trades/KXHIGHNY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wxmm.analysis.trades_ingest import read_trades_parquet
from wxmm.core.errors import CoverageFloorRefused
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import refuse_unless_coverage, trade_anchor_coverage


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
    trades = read_trades_parquet(args.parquet, strict_complement=False)
    mapping = assert_outcome_bookside_mapping([t for t in trades if not t.is_block_trade])
    coverage = trade_anchor_coverage(trades, mapping=mapping, floor=args.floor)
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
