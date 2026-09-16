"""Run C1-M1 v0-MINIMAL on the pulled corpus. Emits scores, never P&L.

Order is fixed and not negotiable: cross-tab gate, then the crossed-state rate
on the real corpus, then coverage, then the walk-forward fit. The crossed rate
runs inside ``run_c1_m1_v0_min`` and refuses an inverted sign before any
parameter is estimated.

No network. Reads the parquet corpus and the CLINYC-derived labels.
Monthly shards are packed one file at a time so the 3.4M-print corpus fits.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from analysis.build_labels import read_labels
from wxmm.analysis.maker_taker import SettlementLabel
from wxmm.analysis.trades_ingest import parquet_shard_paths, read_trades_parquet
from wxmm.backtest.ledger import Ledger
from wxmm.fairvalue.anchor_trades import ObservedMapping
from wxmm.fairvalue.crossed import CrossedDiagnostic, CrossedRate
from wxmm.fairvalue.v0_min import (
    AnchorCoverage,
    ObservationCache,
    V0MinReport,
    run_c1_m1_v0_min,
    trade_anchor_coverage,
)
from wxmm.fairvalue.walkforward import DEFAULT_HOURS_TO_CLOSE

PREREG_PATH = Path("prereg/c1-m1-v0-min.yaml")


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _scope_labels(
    labels: dict[str, SettlementLabel],
    *,
    start: date,
    end: date,
) -> dict[str, SettlementLabel]:
    return {
        ticker: label
        for ticker, label in labels.items()
        if start <= label.climate_day <= end
    }


def _crossed_from_json(path: Path) -> tuple[CrossedDiagnostic, ObservedMapping]:
    raw = json.loads(path.read_text(encoding="utf-8"))

    def rate(payload: dict[str, Any], sign: int) -> CrossedRate:
        return CrossedRate(
            sign=sign,  # type: ignore[arg-type]
            n_prints=int(payload["n_prints"]),
            n_two_sided=int(payload["n_two_sided"]),
            n_crossed=int(payload["n_crossed"]),
            n_touching=int(payload["n_touching"]),
            rate=payload["rate"],
            mean_gap_cents=payload["mean_gap_cents"],
            mean_uncrossed_gap_cents=payload.get("mean_uncrossed_gap_cents"),
            median_gap_cents=payload["median_gap_cents"],
            n_tickers=int(payload["n_tickers"]),
            n_tickers_two_sided=int(payload["n_tickers_two_sided"]),
            n_tickers_majority_crossed=int(payload["n_tickers_majority_crossed"]),
            median_ticker_rate=payload["median_ticker_rate"],
        )

    diag = CrossedDiagnostic(
        as_specified=rate(raw["as_specified"], 1),
        inverted=rate(raw["inverted"], -1),
        verdict=raw["verdict"],
        note=raw["note"],
        n_block_excluded=int(raw["n_block_excluded"]),
    )
    tab = raw["cross_tab"]
    mapping = ObservedMapping(
        counts={
            ("yes", "ask"): int(tab.get("yesxask", 0)),
            ("yes", "bid"): int(tab.get("yesxbid", 0)),
            ("no", "ask"): int(tab.get("noxask", 0)),
            ("no", "bid"): int(tab.get("noxbid", 0)),
        },
        outcome_to_book=dict(raw["outcome_to_book"]),
        n_non_block=sum(int(v) for v in tab.values()),
        status="clean",
    )
    return diag, mapping


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
        f"  climate-day N       {report.n_climate_day_clusters}"
        + ("  (GLM at its limit)" if report.n_climate_day_clusters <= 400 else ""),
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
        "  by horizon vs null:",
    ]
    for row in report.by_horizon:
        lines.append(
            f"    {row.key:<8} n={row.n:<6} mean={row.mean_rps_improvement:+.6f}  "
            f"{_interval(row.ci_low, row.ci_high)}"
        )
    lines.append("  by season vs null:")
    for row in report.by_season:
        lines.append(
            f"    {row.key:<6} n={row.n:<6} mean={row.mean_rps_improvement:+.6f}  "
            f"{_interval(row.ci_low, row.ci_high)}"
        )
    if report.vs_climatology is not None:
        clim = report.vs_climatology
        lines.append("")
        lines.append(
            f"  vs climatology      n={clim.n} mean={clim.mean_rps_improvement:+.6f}  "
            f"{_interval(clim.ci_low, clim.ci_high)}"
        )
    if report.by_horizon_vs_climatology:
        lines.append("  by horizon vs climatology:")
        for row in report.by_horizon_vs_climatology:
            lines.append(
                f"    {row.key:<8} n={row.n:<6} mean={row.mean_rps_improvement:+.6f}  "
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
    parser.add_argument(
        "--crossed",
        type=Path,
        default=Path("analysis/out/crossed_rate.json"),
        help="JSON from analysis.crossed_rate",
    )
    parser.add_argument("--out", type=Path, default=Path("analysis/out/c1_m1_v0_min.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for path in (args.parquet, args.labels, args.prereg, args.crossed):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2
    prereg: dict[str, Any] = yaml.safe_load(args.prereg.read_text(encoding="utf-8"))

    labels = read_labels(args.labels)
    start = args.start or min(label.climate_day for label in labels.values())
    end = args.end or max(label.climate_day for label in labels.values())
    labels = _scope_labels(labels, start=start, end=end)
    days = sorted({label.climate_day for label in labels.values()})
    print(f"scope {start} .. {end}: {len(days)} climate days, {len(labels)} labelled tickers")
    if not days:
        print("nothing in scope", file=sys.stderr)
        return 2

    crossed, mapping = _crossed_from_json(args.crossed)
    print(f"crossed rate {crossed.as_specified.rate} -> {crossed.verdict}")
    if crossed.verdict == "sign_inverted":
        print("STOP 2.1 inverted; refusing to fit", file=sys.stderr)
        return 3

    hours = tuple(int(h) for h in prereg.get("hours_to_close") or DEFAULT_HOURS_TO_CLOSE)
    cache = ObservationCache([], labels, hours_to_close=hours, mapping=mapping)
    n_grid = 0
    n_ok = 0
    n_prints = 0
    labelled_tickers = set(labels)
    labelled_days = set(days)
    seen_days: set[date] = set()
    for shard in parquet_shard_paths(args.parquet):
        trades = read_trades_parquet(shard, strict_complement=False)
        trades = [
            t
            for t in trades
            if t.climate_day in labelled_days and t.ticker in labelled_tickers
        ]
        n_prints += len(trades)
        if not trades:
            continue
        shard_days = {t.climate_day for t in trades}
        seen_days.update(shard_days)
        shard_labels = {
            ticker: lab
            for ticker, lab in labels.items()
            if lab.climate_day in shard_days
        }
        part = trade_anchor_coverage(
            trades, shard_labels, hours_to_close=hours, mapping=mapping
        )
        n_grid += part.n_grid
        n_ok += part.n_both_sides_uncrossed
        cache.ingest(trades)
        cache.warm(sorted(shard_days))
        cache.release_days(sorted(shard_days))
        del trades
        print(
            f"  packed {shard.name} cache={cache.n_cached} "
            f"prints_so_far={n_prints} rss_mb={_rss_mb():.0f}",
            flush=True,
        )
    for climate in labelled_days - seen_days:
        n_on_day = sum(1 for lab in labels.values() if lab.climate_day == climate)
        if n_on_day < 2:
            continue
        n_grid += len(hours)
    coverage = AnchorCoverage(
        n_grid=n_grid,
        n_both_sides_uncrossed=n_ok,
        share=(n_ok / n_grid) if n_grid else 0.0,
    )
    print(f"packed {n_prints} labelled prints, coverage {coverage.share:.4f}")

    report = run_c1_m1_v0_min(
        None,
        labels,
        prereg=prereg,
        prereg_dir=args.prereg_dir,
        ledger=Ledger(),
        n_resample=args.n_resample,
        mapping=mapping,
        crossed=crossed,
        cache=cache,
        coverage=coverage,
    )
    print()
    print(render(report))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
