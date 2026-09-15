"""Run C1-M1 v0-MINIMAL on the pulled corpus. Emits scores, never P&L.

Order is fixed and not negotiable: cross-tab gate, then the crossed-state rate
on the real corpus, then coverage, then the walk-forward fit. The crossed rate
runs inside ``run_c1_m1_v0_min`` and refuses an inverted sign before any
parameter is estimated.

No network. Reads the parquet corpus and the CLINYC-derived labels.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from analysis.build_labels import read_labels
from wxmm.analysis.maker_taker import SettlementLabel
from wxmm.analysis.trades_ingest import RawTrade, read_trades_parquet
from wxmm.backtest.ledger import Ledger
from wxmm.fairvalue.v0_min import V0MinReport, run_c1_m1_v0_min

PREREG_PATH = Path("prereg/c1-m1-v0-min.yaml")


def _scope(
    trades: list[RawTrade],
    labels: dict[str, SettlementLabel],
    *,
    start: date,
    end: date,
) -> tuple[list[RawTrade], dict[str, SettlementLabel]]:
    kept_labels = {
        ticker: label
        for ticker, label in labels.items()
        if start <= label.climate_day <= end
    }
    days = {label.climate_day for label in kept_labels.values()}
    kept_trades = [t for t in trades if t.climate_day in days and t.ticker in kept_labels]
    return kept_trades, kept_labels


def render(report: V0MinReport) -> str:
    crossed = report.crossed.as_specified
    ci = report.clustered_ci
    lines = [
        "C1-M1 v0-MINIMAL  (out-of-sample, walk-forward, never P&L)",
        f"  prereg              {report.prereg_id}",
        "",
        f"  crossed rate        {crossed.rate:.4f} -> {report.crossed.verdict}",
        f"  anchor coverage     {report.coverage.share:.4f} "
        f"({report.coverage.n_both_sides_uncrossed}/{report.coverage.n_grid} grid points)",
        f"  predictions         {report.n_predictions}",
        "",
        f"  mean RPS improvement vs null   {report.mean_rps_improvement:+.6f}",
        f"  day-clustered 95% CI           "
        f"{_interval(*(ci if ci else (None, None)))}",
        f"  contract-level 95% CI          "
        f"{_interval(*(report.contract_ci if report.contract_ci else (None, None)))}",
        "",
        f"  VERDICT             {report.decision.verdict}",
        f"  rule                {report.decision.rule}",
        "",
        "  by season:",
    ]
    for row in report.by_season:
        lines.append(
            f"    {row.key:<6} n={row.n:<6} mean={row.mean_rps_improvement:+.6f}  "
            f"{_interval(row.ci_low, row.ci_high)}"
        )
    lines.append("")
    lines.append("  beta (watch signed_ofi):")
    for coef in report.beta:
        lines.append(
            f"    {coef.name:<28} {coef.point:+.5f}  "
            f"{_interval(coef.ci_low, coef.ci_high)}"
        )
    return "\n".join(lines)


def _interval(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "ci none"
    return f"[{low:+.6f}, {high:+.6f}]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=Path("data/trades/KXHIGHNY"))
    parser.add_argument("--labels", type=Path, default=Path("data/labels/settlement.json"))
    parser.add_argument("--prereg", type=Path, default=PREREG_PATH)
    parser.add_argument("--prereg-dir", type=Path, default=Path("prereg"))
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--n-resample", type=int, default=None)
    parser.add_argument("--out", type=Path, default=Path("analysis/out/c1_m1_v0_min.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for path in (args.parquet, args.labels, args.prereg):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2
    prereg: dict[str, Any] = yaml.safe_load(args.prereg.read_text(encoding="utf-8"))

    labels = read_labels(args.labels)
    trades = read_trades_parquet(args.parquet)
    print(f"loaded {len(trades)} prints, {len(labels)} labelled tickers")

    start = args.start or min(label.climate_day for label in labels.values())
    end = args.end or max(label.climate_day for label in labels.values())
    trades, labels = _scope(trades, labels, start=start, end=end)
    days = sorted({label.climate_day for label in labels.values()})
    print(f"scope {start} .. {end}: {len(days)} climate days, {len(trades)} prints")
    if not days:
        print("nothing in scope", file=sys.stderr)
        return 2

    report = run_c1_m1_v0_min(
        trades,
        labels,
        prereg=prereg,
        prereg_dir=args.prereg_dir,
        ledger=Ledger(),
        n_resample=args.n_resample,
    )
    print()
    print(render(report))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
