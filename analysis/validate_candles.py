"""Test the change-emission hypothesis against the captured candle stream.

Allowed DB: none. Reads raw JSONL only; never opens heartbeat.sqlite or
backfill.sqlite.

The census makes carry-forward its primary statistic, which is safe only if a
tier omits a period because nothing happened, not because data is missing.

Two gates decide that, and failing either blocks the census:

  VOLUME_RECONCILE  - summing per-candle volume must reproduce the market
                      object's lifetime volume. A shortfall is a trade-bearing
                      period the tier dropped, which is exactly what carrying a
                      quote forward across it cannot survive. Compared at the
                      hundredths the API reports, so the test is exact.
  EMPTYING_EMITTED  - a tier must emit a candle when a book goes empty. With no
                      two-sided-to-empty transition, carry-forward would hold a
                      dead book alive indefinitely.

Two measurements are reported alongside them but gate nothing:

  TIER_GAPS         - inter-candle gap distribution per tier.
  STALENESS_DELTA   - in-window coverage under both staleness rules, per
                      horizon, on the live tier.

STALENESS_DELTA was designed as a control: if the live tier were the dense
one-candle-per-minute series that venue-facts 1.7 assumed, the two rules would
select the same snapshots and any divergence would be attributable to the
historical tier alone. That premise is testable and the script prints whether it
holds. Where it does not, the control is unavailable and VOLUME_RECONCILE
carries the argument by itself; the script says so rather than reporting a
verdict the evidence does not support.

No thresholds are invented. Both gates are an exact equality or an existence
claim, and the two measurements are reported without a pass mark.

Run this only after a completed bulk fetch. A market whose candles are still
being fetched is indistinguishable here from one whose candles are missing, and
would register as a volume shortfall.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.spread_census import (
    HORIZONS_H,
    build_snapshot_table,
    candle_fields,
    is_two_sided,
    iter_category,
    load_markets,
    ticker_climate_date,
)
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)

LIVE_TIER = "live"
HISTORICAL_TIER = "historical"
LIVE_PERIOD_SEC = 60
STALE_SEC = 15 * 60
SAMPLE_MARKETS = 3


def tier_of(endpoint: str) -> str:
    return HISTORICAL_TIER if "/historical/" in str(endpoint) else LIVE_TIER


def load_candles_by_tier(raw_dir: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Return {tier: {ticker: [candle_fields, ...]}} in emission order."""
    by_tier: dict[str, dict[str, list[dict[str, Any]]]] = {
        LIVE_TIER: defaultdict(list),
        HISTORICAL_TIER: defaultdict(list),
    }
    for record in iter_category(raw_dir, "candlesticks"):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        endpoint = str(record.get("endpoint") or "")
        ticker = str(payload.get("ticker") or "")
        if not ticker:
            parts = endpoint.strip("/").split("/")
            if "markets" in parts:
                idx = parts.index("markets")
                if idx + 1 < len(parts) and parts[idx + 1] != "candlesticks":
                    ticker = parts[idx + 1]
        candles = payload.get("candlesticks")
        if not ticker or not isinstance(candles, list):
            continue
        bucket = by_tier[tier_of(endpoint)][ticker]
        for candle in candles:
            if isinstance(candle, dict):
                bucket.append(candle_fields(candle))
    return {tier: dict(tickers) for tier, tickers in by_tier.items()}


def _sorted_unique(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chunked fetches overlap at window edges, so dedupe on end_period_ts."""
    by_ts: dict[int, dict[str, Any]] = {}
    for candle in candles:
        end_ts = candle.get("end_period_ts")
        if isinstance(end_ts, (int, float)):
            by_ts[int(end_ts)] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def gap_distribution(candles_by_ticker: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    gaps: list[int] = []
    n_candles = 0
    for candles in candles_by_ticker.values():
        ordered = _sorted_unique(candles)
        n_candles += len(ordered)
        gaps.extend(
            int(b["end_period_ts"]) - int(a["end_period_ts"]) for a, b in zip(ordered, ordered[1:])
        )
    if not gaps:
        return {
            "n_markets": len(candles_by_ticker),
            "n_candles": n_candles,
            "n_gaps": 0,
            "modal_gap_sec": None,
            "median_gap_sec": None,
            "p90_gap_sec": None,
            "share_gap_over_one_period": None,
            "share_gap_over_15min": None,
        }
    series = pd.Series(gaps, dtype="int64")
    return {
        "n_markets": len(candles_by_ticker),
        "n_candles": n_candles,
        "n_gaps": len(gaps),
        "modal_gap_sec": int(statistics.mode(gaps)),
        "median_gap_sec": float(series.median()),
        "p90_gap_sec": float(series.quantile(0.90)),
        # Zero here, and only zero, means the tier emits every period regardless
        # of activity. A modal gap of one period does not establish that.
        "share_gap_over_one_period": float((series > LIVE_PERIOD_SEC).mean()),
        "share_gap_over_15min": float((series > STALE_SEC).mean()),
    }


def live_variant_agreement(
    *,
    markets: list[dict[str, Any]],
    live_candles: dict[str, list[dict[str, Any]]],
) -> pd.DataFrame:
    """Per-horizon in-window coverage under both staleness rules, live tier only."""
    live_markets = [m for m in markets if str(m.get("ticker") or "") in live_candles]
    if not live_markets:
        return pd.DataFrame()
    snapshots, _stats = build_snapshot_table(
        markets=live_markets,
        candles_by_ticker=live_candles,
        labels=pd.DataFrame(),
    )
    in_window = snapshots[snapshots["in_trading_window"]]
    if in_window.empty:
        return pd.DataFrame()
    rows = []
    for horizon in sorted(set(HORIZONS_H), reverse=True):
        group = in_window[in_window["horizon_h"] == horizon]
        if group.empty:
            continue
        strict = float(group["quote_valid"].mean())
        carry = float(group["quote_present"].mean())
        rows.append(
            {
                "horizon_h": horizon,
                "n_snapshots": int(len(group)),
                "coverage_strict15": strict,
                "coverage_carryforward": carry,
                "abs_difference": abs(carry - strict),
            }
        )
    return pd.DataFrame(rows)


def _same_quantity(left: float, right: float) -> bool:
    """Compare volumes at their own resolution.

    The API reports quantities as fixed-point strings with two decimals, so
    hundredths are the exact grain of the data. Summing thousands of floats
    otherwise leaves 1e-11 residue that would read as a dropped trade.
    """
    return round(left * 100) == round(right * 100)


def _market_volume(market: dict[str, Any]) -> float | None:
    for key in ("volume_fp", "volume"):
        raw = market.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def volume_reconciliation(
    *,
    markets: list[dict[str, Any]],
    candles_by_tier: dict[str, dict[str, list[dict[str, Any]]]],
) -> pd.DataFrame:
    rows = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        tier = LIVE_TIER if ticker in candles_by_tier[LIVE_TIER] else HISTORICAL_TIER
        candles = candles_by_tier[tier].get(ticker)
        if not candles:
            continue
        market_volume = _market_volume(market)
        if market_volume is None:
            continue
        ordered = _sorted_unique(candles)
        volumes = [c["volume"] for c in ordered if c["volume"] is not None]
        candle_sum = float(sum(volumes))
        # Reported alongside the sum because a per-candle field that is secretly
        # cumulative would otherwise produce an uninterpretable mismatch.
        candle_max = float(max(volumes)) if volumes else 0.0
        rows.append(
            {
                "ticker": ticker,
                "tier": tier,
                "market_volume": market_volume,
                "candle_volume_sum": candle_sum,
                "candle_volume_max": candle_max,
                "sum_difference": candle_sum - market_volume,
                "sum_matches": _same_quantity(candle_sum, market_volume),
                "max_matches": _same_quantity(candle_max, market_volume),
            }
        )
    return pd.DataFrame(rows)


def emptying_events(
    candles_by_ticker: dict[str, list[dict[str, Any]]],
) -> tuple[pd.DataFrame, dict[str, list[dict[str, Any]]]]:
    """Count empty-book candles and two-sided-to-empty transitions per market."""
    rows = []
    samples: dict[str, list[dict[str, Any]]] = {}
    for ticker in sorted(candles_by_ticker):
        ordered = _sorted_unique(candles_by_ticker[ticker])
        empty = 0
        transitions = 0
        previous_two_sided = False
        transition_rows: list[dict[str, Any]] = []
        for candle in ordered:
            two_sided = is_two_sided(candle["bid_close"], candle["ask_close"])
            is_empty = not two_sided and candle["bid_close"] is not None
            if is_empty:
                empty += 1
            if previous_two_sided and is_empty:
                transitions += 1
                transition_rows.append(candle)
            previous_two_sided = two_sided
        rows.append(
            {
                "ticker": ticker,
                "n_candles": len(ordered),
                "n_empty_book_candles": empty,
                "n_two_sided_to_empty": transitions,
            }
        )
        if transition_rows and len(samples) < SAMPLE_MARKETS:
            samples[ticker] = transition_rows[:5]
    return pd.DataFrame(rows), samples


def _verdict(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def run(config: dict[str, Any]) -> int:
    raw_dir = Path(config["storage"]["raw_dir"])
    markets = load_markets(raw_dir)
    candles_by_tier = load_candles_by_tier(raw_dir)
    live = candles_by_tier[LIVE_TIER]
    historical = candles_by_tier[HISTORICAL_TIER]
    failures: list[str] = []

    print("=== MEASUREMENT TIER_GAPS: inter-candle gaps by tier ===")
    tier_gaps: dict[str, dict[str, Any]] = {}
    for tier in (LIVE_TIER, HISTORICAL_TIER):
        tier_gaps[tier] = gap_distribution(candles_by_tier[tier])
        print(f"{tier}: {tier_gaps[tier]}")
    live_unconditional = tier_gaps[LIVE_TIER]["share_gap_over_one_period"] == 0.0
    print(
        "live tier emits one candle per minute unconditionally: "
        f"{live_unconditional} "
        f"(share of live gaps longer than {LIVE_PERIOD_SEC}s = "
        f"{tier_gaps[LIVE_TIER]['share_gap_over_one_period']})"
    )
    if not live_unconditional:
        print(
            "Both tiers therefore emit on change, and no dense control exists. "
            "This revises venue-facts 1.7, which read sparseness as a property "
            "of the historical tier; it is a property of quiet markets."
        )

    print()
    print("=== MEASUREMENT STALENESS_DELTA: in-window coverage by rule (live tier) ===")
    agreement = live_variant_agreement(markets=markets, live_candles=live)
    if agreement.empty:
        max_difference: float | None = None
        print("no in-window live-tier snapshots; nothing to compare")
    else:
        print(agreement.to_string(index=False))
        max_difference = float(agreement["abs_difference"].max())
    print(f"max |coverage_carryforward - coverage_strict15| = {max_difference}")
    if not live_unconditional:
        print(
            "Not usable as a control: the divergence above is the live tier's "
            "own change-emission, not evidence about the historical tier."
        )

    print()
    print("=== GATE VOLUME_RECONCILE: summed candle volume vs lifetime volume ===")
    reconciliation = volume_reconciliation(markets=markets, candles_by_tier=candles_by_tier)
    if reconciliation.empty:
        volumes_match = False
        print("no market had both a lifetime volume and candles; cannot evaluate")
    else:
        mismatched = reconciliation[~reconciliation["sum_matches"]]
        volumes_match = mismatched.empty
        for tier, group in reconciliation.groupby("tier"):
            bad = group[~group["sum_matches"]]
            print(
                f"{tier}: markets={len(group)} sum_mismatches={len(bad)} "
                f"total_abs_shortfall={float(bad['sum_difference'].abs().sum()):.2f} "
                f"cumulative_field_matches={int(group['max_matches'].sum())}"
            )
        total_volume = float(reconciliation["market_volume"].sum())
        unreconciled = float(mismatched["sum_difference"].abs().sum())
        print(
            f"overall: {len(reconciliation) - len(mismatched)}/{len(reconciliation)} markets "
            f"reconcile exactly; {unreconciled:.2f} of {total_volume:.2f} contracts "
            f"unaccounted ({unreconciled / total_volume:.8%})"
        )
        if not mismatched.empty:
            short = int((mismatched["sum_difference"] < 0).sum())
            excess = int((mismatched["sum_difference"] > 0).sum())
            dates = mismatched["ticker"].map(ticker_climate_date).nunique()
            print(
                f"direction: {short} markets short of the lifetime volume, "
                f"{excess} above it, across {dates} climate days"
            )
            if excess:
                # Omitted candles can only lose volume. Gaining it means the two
                # venue-side fields disagree, which no fetch strategy can fix.
                print(
                    "A candle sum above the lifetime volume cannot come from omitted "
                    "candles, so at least some of this is venue bookkeeping rather "
                    "than missing capture."
                )
        if not mismatched.empty:
            print("worst 10 mismatches by absolute difference:")
            worst = mismatched.reindex(
                mismatched["sum_difference"].abs().sort_values(ascending=False).index
            )
            print(worst.head(10).to_string(index=False))
    if not volumes_match:
        failures.append("VOLUME_RECONCILE")
    print(f"GATE VOLUME_RECONCILE: {_verdict(volumes_match)}")

    print()
    print("=== GATE EMPTYING_EMITTED: historical-tier emptying events ===")
    events, samples = emptying_events(historical)
    if events.empty:
        emptying_emitted = False
        print("no historical-tier candles; cannot evaluate")
    else:
        total_empty = int(events["n_empty_book_candles"].sum())
        total_transitions = int(events["n_two_sided_to_empty"].sum())
        markets_with_transition = int((events["n_two_sided_to_empty"] > 0).sum())
        emptying_emitted = total_transitions > 0
        print(
            f"markets={len(events)} empty_book_candles={total_empty} "
            f"two_sided_to_empty_transitions={total_transitions} "
            f"markets_with_a_transition={markets_with_transition}"
        )
        for ticker, transitions in samples.items():
            print(f"sample {ticker}:")
            for candle in transitions:
                print(
                    f"  end_period_ts={candle['end_period_ts']} "
                    f"bid={candle['bid_close']} ask={candle['ask_close']} "
                    f"volume={candle['volume']}"
                )
    if not emptying_emitted:
        failures.append("EMPTYING_EMITTED")
    print(f"GATE EMPTYING_EMITTED: {_verdict(emptying_emitted)}")

    print()
    if failures:
        print(f"OVERALL: FAIL ({', '.join(failures)})")
        print(
            "A failed gate blocks the census: carry-forward as the primary "
            "statistic assumes omitted periods are uneventful."
        )
        return 1
    print("OVERALL: PASS")
    print(
        "Omitted periods carry no volume and emptying is emitted, so carrying "
        "the last quote forward reproduces the book rather than inventing one."
    )
    if not live_unconditional:
        print(
            "Qualification: both tiers emit on change, so this rests on the "
            "volume reconciliation alone. There is no dense tier to cross-check "
            "it against."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate candle emission semantics")
    parser.add_argument("--config", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(load_config(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
