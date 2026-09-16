"""Load v0 ingest artifacts and emit cross-tab + coverage. Does not fit."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from analysis.v0_io import latest_trades_path, load_trades
from wxmm.analysis.maker_taker import season_of
from wxmm.analysis.trades_ingest import parse_climate_day
from wxmm.core.errors import InconsistentTakerMapping
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import DEFAULT_COVERAGE_FLOOR, trade_anchor_coverage


def universe_from_markets(path: Path) -> tuple[list[tuple[str, date]], dict[str, str]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[tuple[str, date]] = []
    roles: dict[str, str] = {}
    for row in rows:
        ticker = str(row["ticker"])
        climate = parse_climate_day(ticker)
        pairs.append((ticker, climate))
        strike = row.get("strike_type")
        if strike in {"greater", "less", "between"}:
            roles[ticker] = str(strike)
    return pairs, roles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v0 cross-tab + coverage on ingested trades")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out/v0"))
    parser.add_argument("--floor", type=float, default=DEFAULT_COVERAGE_FLOOR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trades_path = latest_trades_path(args.out_dir)
    markets_path = args.out_dir / "markets.json"
    trades = load_trades(trades_path)
    universe, roles = universe_from_markets(markets_path)
    try:
        mapping = assert_outcome_bookside_mapping(
            [t for t in trades if not t.is_block_trade]
        )
        mapping_payload: dict[str, Any] = {
            "status": "clean",
            "outcome_to_book": mapping.outcome_to_book,
            "counts": {f"{a}x{b}": n for (a, b), n in mapping.counts.items()},
            "n_non_block": mapping.n_non_block,
        }
    except InconsistentTakerMapping as exc:
        payload = {
            "mapping": {"status": "INCONSISTENT", "reason": str(exc)},
            "n_trades": len(trades),
        }
        (args.out_dir / "coverage.json").write_text(json.dumps(payload, indent=2) + "\n")
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    coverage = trade_anchor_coverage(
        trades,
        mapping=mapping,
        floor=args.floor,
        universe=universe,
        roles=roles,
    )
    by_season = {
        row.key: {
            "share": row.share,
            "n_grid": row.n_grid,
            "n_two_sided": row.n_two_sided_uncrossed,
        }
        for row in coverage.by_season
    }
    payload = {
        "mapping": mapping_payload,
        "n_trades": len(trades),
        "n_universe": len(universe),
        "coverage": coverage.as_dict(),
        "two_sided_share": coverage.two_sided_share,
        "by_season": by_season,
        "floor": args.floor,
        "below_floor": coverage.two_sided_share < args.floor,
        "seasons_present": sorted({season_of(p[1]) for p in universe}),
    }
    (args.out_dir / "coverage.json").write_text(json.dumps(payload, indent=2) + "\n")
    json.dump(
        {
            "mapping": mapping_payload,
            "two_sided_share": coverage.two_sided_share,
            "by_season": by_season,
            "n_grid": coverage.n_grid,
            "n_two_sided_uncrossed": coverage.n_two_sided_uncrossed,
            "below_floor": payload["below_floor"],
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
