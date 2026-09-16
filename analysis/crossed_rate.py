"""Report the crossed-state rate on the real corpus. Run before any fit.

The cross-tab cannot verify the direction sign: its off-diagonal is exactly
zero, so ``taker_book_side`` is redundant with ``taker_outcome_side``. This is
the independent check. A synthetic fixture cannot substitute, because the
fixture would encode the assumption under test.

Reads parquet written by ``analysis.pull_corpus``. No network, no fit.
Monthly shards are processed one file at a time so the 3.4M-print corpus
fits in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from wxmm.analysis.trades_ingest import parquet_shard_paths, read_trades_parquet
from wxmm.core.errors import InconsistentTakerMapping
from wxmm.fairvalue.anchor_trades import ObservedMapping
from wxmm.fairvalue.crossed import (
    CrossedAccumulator,
    CrossedDiagnostic,
    _verdict,
    first_and_last_print,
)


def _mapping_from_counts(counts: dict[tuple[str, str], int], n: int) -> ObservedMapping:
    filled = {
        ("yes", "ask"): counts.get(("yes", "ask"), 0),
        ("yes", "bid"): counts.get(("yes", "bid"), 0),
        ("no", "ask"): counts.get(("no", "ask"), 0),
        ("no", "bid"): counts.get(("no", "bid"), 0),
    }
    if n == 0:
        raise InconsistentTakerMapping("no non-block trades to verify mapping")
    yes_ask, yes_bid = filled[("yes", "ask")], filled[("yes", "bid")]
    no_ask, no_bid = filled[("no", "ask")], filled[("no", "bid")]
    if yes_ask and yes_bid:
        raise InconsistentTakerMapping(
            f"yes maps to both ask ({yes_ask}) and bid ({yes_bid}); do not pick a frame"
        )
    if no_ask and no_bid:
        raise InconsistentTakerMapping(
            f"no maps to both ask ({no_ask}) and bid ({no_bid}); do not pick a frame"
        )
    if yes_ask and no_ask:
        raise InconsistentTakerMapping("ask maps to both yes and no")
    if yes_bid and no_bid:
        raise InconsistentTakerMapping("bid maps to both yes and no")
    diagonal = yes_ask > 0 and no_bid > 0 and yes_bid == 0 and no_ask == 0
    anti = yes_bid > 0 and no_ask > 0 and yes_ask == 0 and no_bid == 0
    if not (diagonal or anti):
        raise InconsistentTakerMapping(
            f"cross-tab is not a complete one-to-one bijection: {filled}"
        )
    outcome_to_book = {"yes": "ask", "no": "bid"} if diagonal else {"yes": "bid", "no": "ask"}
    return ObservedMapping(
        counts=filled,
        outcome_to_book=outcome_to_book,
        n_non_block=n,
        status="clean",
    )


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
        f"  mean uncrossed gap  {diag.as_specified.mean_uncrossed_gap_cents} c "
        f"(ask − bid on uncrossed points)",
        "",
        f"  VERDICT           {diag.verdict}",
        f"  {diag.note}",
    ]
    return "\n".join(lines)


def run_on_shards(
    parquet: Path,
    *,
    start: date | None = None,
    end: date | None = None,
    collect_by_day: bool = False,
) -> tuple[CrossedDiagnostic, ObservedMapping, int, str, dict[str, float]]:
    shards = parquet_shard_paths(parquet)
    if not shards:
        raise FileNotFoundError(f"no parquet in {parquet}")
    specified = CrossedAccumulator(sign=1)
    inverted = CrossedAccumulator(sign=-1)
    counts: dict[tuple[str, str], int] = Counter()
    n_non_block = 0
    n_trades = 0
    first: datetime | None = None
    last: datetime | None = None
    by_day: dict[str, float] = {}
    for shard in shards:
        trades = read_trades_parquet(shard, strict_complement=False)
        if start is not None or end is not None:
            trades = [
                t
                for t in trades
                if (start is None or t.climate_day >= start)
                and (end is None or t.climate_day <= end)
            ]
        if not trades:
            continue
        n_trades += len(trades)
        specified.add(trades)
        inverted.add(trades)
        for trade in trades:
            if trade.is_block_trade:
                continue
            n_non_block += 1
            counts[(trade.taker_outcome_side, trade.taker_book_side)] += 1
        bounds = first_and_last_print(trades)
        if bounds is not None:
            lo, hi = bounds
            first = lo if first is None else min(first, lo)
            last = hi if last is None else max(last, hi)
        if collect_by_day:
            from wxmm.fairvalue.crossed import crossed_state_rate

            buckets: dict[str, list[object]] = {}
            for trade in trades:
                buckets.setdefault(trade.climate_day.isoformat(), []).append(trade)
            for day, rows in buckets.items():
                result = crossed_state_rate(rows, sign=1)  # type: ignore[arg-type]
                if result.rate is not None:
                    by_day[day] = result.rate
        del trades
    if n_trades == 0:
        raise FileNotFoundError("corpus is empty after the date filter")
    as_specified = specified.finish()
    inv = inverted.finish()
    verdict, note = _verdict(as_specified, inv)
    diag = CrossedDiagnostic(
        as_specified=as_specified,
        inverted=inv,
        verdict=verdict,
        note=note,
        n_block_excluded=specified.n_block_excluded,
    )
    mapping = _mapping_from_counts(dict(counts), n_non_block)
    span = (
        f"{first.isoformat()} .. {last.isoformat()}" if first and last else "unknown span"
    )
    return diag, mapping, n_trades, span, dict(sorted(by_day.items()))


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
    try:
        diag, mapping, n_trades, span, by_day = run_on_shards(
            args.parquet,
            start=args.start,
            end=args.end,
            collect_by_day=args.by_day_out is not None,
        )
    except (FileNotFoundError, InconsistentTakerMapping) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        "CROSS-TAB (gate only; redundant with taker_outcome_side, verifies nothing): "
        + json.dumps({f"{k[0]}x{k[1]}": v for k, v in mapping.counts.items()})
    )
    print()
    print(render(diag, n_trades=n_trades, span=span))
    rate = diag.as_specified.rate
    if rate is not None and rate > 0.5:
        print(
            f"\nSTOP 2.1 HALT: crossed rate {rate:.4f} > 0.5; mapping is inverted.",
            file=sys.stderr,
        )

    if args.json_out is not None:
        payload = {
            "n_trades": n_trades,
            "span": span,
            "cross_tab": {f"{k[0]}x{k[1]}": v for k, v in mapping.counts.items()},
            "outcome_to_book": mapping.outcome_to_book,
            "as_specified": asdict(diag.as_specified),
            "inverted": asdict(diag.inverted),
            "verdict": diag.verdict,
            "note": diag.note,
            "n_block_excluded": diag.n_block_excluded,
            "halt_if_rate_gt_50pct": bool(rate is not None and rate > 0.5),
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    if args.by_day_out is not None:
        args.by_day_out.parent.mkdir(parents=True, exist_ok=True)
        args.by_day_out.write_text(json.dumps(by_day, indent=2), encoding="utf-8")
        print(f"wrote {args.by_day_out}")

    if rate is not None and rate > 0.5:
        return 3
    return 0 if diag.verdict == "sign_confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
