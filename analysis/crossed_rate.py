"""Report the crossed-state rate on the real corpus. Run before any fit.

The cross-tab cannot verify the direction sign: its off-diagonal is exactly
zero, so ``taker_book_side`` is redundant with ``taker_outcome_side``. This is
the independent check. A synthetic fixture cannot substitute, because the
fixture would encode the assumption under test.

Reads parquet written by ``analysis.pull_corpus``. No network, no fit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from wxmm.analysis.trades_ingest import RawTrade, read_trades_parquet
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.crossed import (
    CrossedDiagnostic,
    crossed_by_climate_day,
    crossed_diagnostic,
    first_and_last_print,
)


def _in_range(trade: RawTrade, start: date | None, end: date | None) -> bool:
    if start is not None and trade.climate_day < start:
        return False
    if end is not None and trade.climate_day > end:
        return False
    return True


def render(diag: CrossedDiagnostic, *, n_trades: int, span: str) -> str:
    lines = [
        "CROSSED-STATE RATE  (independent check on the direction sign)",
        f"  corpus            {n_trades} prints, {span}",
        f"  block excluded    {diag.n_block_excluded}",
        "",
        f"  as specified  {diag.as_specified.summary}",
        f"    per-ticker      median {diag.as_specified.median_ticker_rate}, "
        f"{diag.as_specified.n_tickers_majority_crossed}/"
        f"{diag.as_specified.n_tickers_two_sided} tickers majority-crossed",
        f"  inverted d    {diag.inverted.summary}",
        "    (exact mirror up to ties; shown for reading, not as corroboration)",
        "",
        f"  VERDICT           {diag.verdict}",
        f"  {diag.note}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--by-day-out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.parquet.exists():
        print(f"no corpus at {args.parquet}; run analysis.pull_corpus first", file=sys.stderr)
        return 2
    trades = [
        t for t in read_trades_parquet(args.parquet) if _in_range(t, args.start, args.end)
    ]
    if not trades:
        print("corpus is empty after the date filter", file=sys.stderr)
        return 2

    non_block = [t for t in trades if not t.is_block_trade]
    mapping = assert_outcome_bookside_mapping(non_block)
    bounds = first_and_last_print(trades)
    span = (
        f"{bounds[0].isoformat()} .. {bounds[1].isoformat()}" if bounds else "unknown span"
    )
    diag = crossed_diagnostic(trades)
    print(
        "CROSS-TAB (gate only; redundant with taker_outcome_side, verifies nothing): "
        + json.dumps({f"{k[0]}x{k[1]}": v for k, v in mapping.counts.items()})
    )
    print()
    print(render(diag, n_trades=len(trades), span=span))

    if args.json_out is not None:
        payload = {
            "n_trades": len(trades),
            "span": span,
            "cross_tab": {f"{k[0]}x{k[1]}": v for k, v in mapping.counts.items()},
            "outcome_to_book": mapping.outcome_to_book,
            "as_specified": asdict(diag.as_specified),
            "inverted": asdict(diag.inverted),
            "verdict": diag.verdict,
            "note": diag.note,
            "n_block_excluded": diag.n_block_excluded,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    if args.by_day_out is not None:
        by_day = crossed_by_climate_day(trades)
        args.by_day_out.parent.mkdir(parents=True, exist_ok=True)
        args.by_day_out.write_text(json.dumps(by_day, indent=2), encoding="utf-8")
        print(f"wrote {args.by_day_out}")

    return 0 if diag.verdict == "sign_confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
