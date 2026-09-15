"""Run the v0 coverage pre-check. Does not fit.

python -m analysis.v0_coverage --trades path.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import refuse_unless_coverage, trade_anchor_coverage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C1-M1 v0 trade-anchor coverage")
    parser.add_argument("--floor", type=float, default=0.15)
    parser.add_argument(
        "--trades-json",
        type=Path,
        default=None,
        help="unused placeholder; inject trades in-process or via a future parquet reader",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.trades_json is None:
        json.dump(
            {
                "status": "NOT_RUN",
                "reason": "no trade corpus on disk; coverage is computed by run_c1_m1_v0",
                "floor": args.floor,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    raise SystemExit("in-process coverage only; pass trades to trade_anchor_coverage")


# Re-export for tests / notebooks.
__all__ = [
    "assert_outcome_bookside_mapping",
    "refuse_unless_coverage",
    "trade_anchor_coverage",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
