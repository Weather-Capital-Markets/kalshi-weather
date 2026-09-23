"""Walk-forward v0 score on ingested trades + CLINYC labels.

Not gated on X1 FILL_IN. Decision is the sign test: OOS RPS vs null
with a day-clustered interval excluding zero.

python -m analysis.v0_score --out-dir analysis/out/v0
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from analysis.v0_io import latest_trades_path, load_trades
from analysis.v0_labels import labels_from_clinyc
from wxmm.backtest.ledger import Ledger, refuse_unless_preregistered
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import (
    CoverageSlice,
    TradeAnchorCoverage,
    refuse_unless_coverage,
)
from wxmm.fairvalue.v0 import precompute_v0_rows, score_cached_walkforward
from wxmm.fairvalue.walkforward import DEFAULT_HOURS_TO_CLOSE


def coverage_from_payload(payload: dict[str, Any]) -> TradeAnchorCoverage:
    raw = payload["coverage"]

    def slices(key: str) -> tuple[CoverageSlice, ...]:
        return tuple(
            CoverageSlice(
                key=str(row["key"]),
                n_grid=int(row["n_grid"]),
                n_two_sided_uncrossed=int(row["n_two_sided_uncrossed"]),
                n_one_sided=int(row["n_one_sided"]),
                n_missing=int(row["n_missing"]),
                n_crossed_resolved=int(row["n_crossed_resolved"]),
                share=float(row["share"]),
            )
            for row in raw[key]
        )

    return TradeAnchorCoverage(
        n_grid=int(raw["n_grid"]),
        n_two_sided_uncrossed=int(raw["n_two_sided_uncrossed"]),
        n_one_sided=int(raw["n_one_sided"]),
        n_missing=int(raw["n_missing"]),
        n_crossed_resolved=int(raw["n_crossed_resolved"]),
        two_sided_share=float(raw["two_sided_share"]),
        crossed_rate=float(raw["crossed_rate"]),
        floor=float(raw["floor"]),
        by_season=slices("by_season"),
        by_hours_to_close=slices("by_hours_to_close"),
        by_bracket_role=slices("by_bracket_role"),
        bid_staleness_seconds=(),
        ask_staleness_seconds=(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C1-M1 v0 walk-forward score")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out/v0"))
    parser.add_argument("--prereg", type=Path, default=Path("prereg/c1-m1-v0.yaml"))
    parser.add_argument("--prereg-dir", type=Path, default=Path("prereg"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir
    coverage_payload = json.loads((out_dir / "coverage.json").read_text(encoding="utf-8"))
    if coverage_payload.get("mapping", {}).get("status") != "clean":
        print("mapping is not clean; do not score", file=sys.stderr)
        return 2
    coverage = coverage_from_payload(coverage_payload)
    refuse_unless_coverage(coverage, floor=float(coverage_payload.get("floor", 0.15)))

    prereg = yaml.safe_load(args.prereg.read_text(encoding="utf-8"))
    registered = refuse_unless_preregistered(prereg, args.prereg_dir)
    hours = tuple(int(h) for h in registered.get("hours_to_close") or DEFAULT_HOURS_TO_CLOSE)
    min_train = int(registered.get("walk_forward", {}).get("min_train_days") or 30)
    ridge = float(registered.get("ridge_lambda", 1.0))
    seed = int(registered.get("seed", 0))
    boot_n = int(registered.get("bootstrap", {}).get("n_resample") or 1000)

    markets = json.loads((out_dir / "markets.json").read_text(encoding="utf-8"))
    clinyc = json.loads((out_dir / "clinyc.json").read_text(encoding="utf-8"))
    labels, label_diag = labels_from_clinyc(markets, clinyc["labels"])
    print(json.dumps({"labels": label_diag}), file=sys.stderr)

    trades_path = latest_trades_path(out_dir)
    print(f"loading {trades_path}", file=sys.stderr)
    trades = load_trades(trades_path)
    mapping = assert_outcome_bookside_mapping(
        [trade for trade in trades if not trade.is_block_trade]
    )
    print(f"precompute n_trades={len(trades)} n_labels={len(labels)}", file=sys.stderr)
    rows, imputation = precompute_v0_rows(
        trades, labels, hours_to_close=hours, mapping=mapping
    )
    print(f"complete_ladder_rows={len(rows)}", file=sys.stderr)
    report = score_cached_walkforward(
        rows,
        labels,
        coverage=coverage,
        prereg=registered,
        ledger=Ledger(),
        n_resample=boot_n,
        ridge_lambda=ridge,
        min_train_days=min_train,
        hours_to_close=hours,
        seed=seed,
        climatology_doy_window=int(registered.get("climatology_doy_window", 15)),
        imputation=imputation.as_dict(),
    )
    payload = report.as_dict()
    payload["label_diagnostics"] = label_diag
    payload["n_complete_ladder_rows"] = len(rows)
    payload["n_trades"] = len(trades)
    payload["trades_path"] = str(trades_path)
    payload["mapping"] = coverage_payload["mapping"]
    (out_dir / "score.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    summary = {
        "mapping": coverage_payload["mapping"],
        "two_sided_share": coverage.two_sided_share,
        "by_season_coverage": {
            row.key: row.share for row in coverage.by_season
        },
        "n_predictions": report.n_predictions,
        "mean_rps_improvement": report.mean_rps_improvement,
        "clustered_ci": report.clustered_ci,
        "decision": report.decision.verdict,
        "by_season": [asdict(s) for s in report.by_season],
        "label_diagnostics": label_diag,
        "n_complete_ladder_rows": len(rows),
        "is_strategy_pnl": False,
    }
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
