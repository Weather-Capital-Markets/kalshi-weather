"""Test the change-emission hypothesis against the captured candle stream.

Allowed DB: none. Reads raw JSONL only; never opens heartbeat.sqlite or
backfill.sqlite.

The census makes carry-forward its primary statistic, which is only safe if the
sparse historical tier omits periods because nothing happened rather than
because data is missing. Four checks, each with a failure condition named up
front so a FAIL means something specific:

  1. LIVE_TIER_DENSITY   - the live tier is documented as one candle per minute.
                           FAILS if its modal inter-candle gap is not 60 s,
                           because every other check leans on the live tier as
                           the dense control.
  2. LIVE_VARIANT_AGREE  - on a dense tier, carrying a quote forward and
                           requiring it to be under 15 minutes old must select
                           the same snapshots. FAILS if in-window coverage
                           differs at any horizon, which would mean the live
                           tier is itself sparse and the control is invalid.
  3. VOLUME_RECONCILE    - summing per-candle volume must reproduce the market
                           object's lifetime volume. FAILS on any mismatch: a
                           shortfall is a trade-bearing period the tier dropped,
                           which is exactly what carry-forward cannot survive.
  4. EMPTYING_EMITTED    - the historical tier must emit a candle when a book
                           goes empty. FAILS if no two-sided-to-empty transition
                           is observed, because carry-forward would then hold a
                           dead book alive indefinitely.

No thresholds are invented. Every condition above is an exact equality or an
existence claim.
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
                "sum_matches": candle_sum == market_volume,
                "max_matches": candle_max == market_volume,
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

    print("=== tier gap distributions ===")
    for tier in (LIVE_TIER, HISTORICAL_TIER):
        stats = gap_distribution(candles_by_tier[tier])
        print(f"{tier}: {stats}")
        if tier == LIVE_TIER:
            live_gaps = stats
    live_dense = live_gaps["modal_gap_sec"] == LIVE_PERIOD_SEC
    if not live_dense:
        failures.append("LIVE_TIER_DENSITY")
    print(
        f"CHECK LIVE_TIER_DENSITY: {_verdict(live_dense)} "
        f"(modal live gap {live_gaps['modal_gap_sec']}s, expected {LIVE_PERIOD_SEC}s)"
    )

    print()
    print("=== live-tier staleness-variant agreement (in-window snapshots) ===")
    agreement = live_variant_agreement(markets=markets, live_candles=live)
    if agreement.empty:
        variants_agree = False
        max_difference: float | None = None
        print("no in-window live-tier snapshots; cannot evaluate")
    else:
        print(agreement.to_string(index=False))
        max_difference = float(agreement["abs_difference"].max())
        variants_agree = max_difference == 0.0
    if not variants_agree:
        failures.append("LIVE_VARIANT_AGREE")
    print(
        f"CHECK LIVE_VARIANT_AGREE: {_verdict(variants_agree)} "
        f"(max |coverage_carryforward - coverage_strict15| = {max_difference})"
    )

    print()
    print("=== volume reconciliation ===")
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
        if not mismatched.empty:
            print("worst 10 mismatches by absolute difference:")
            worst = mismatched.reindex(
                mismatched["sum_difference"].abs().sort_values(ascending=False).index
            )
            print(worst.head(10).to_string(index=False))
    if not volumes_match:
        failures.append("VOLUME_RECONCILE")
    print(f"CHECK VOLUME_RECONCILE: {_verdict(volumes_match)}")

    print()
    print("=== historical-tier emptying events ===")
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
    print(f"CHECK EMPTYING_EMITTED: {_verdict(emptying_emitted)}")

    print()
    if failures:
        print(f"OVERALL: FAIL ({', '.join(failures)})")
        print(
            "A failure here blocks the census: carry-forward as the primary "
            "statistic assumes omitted periods are uneventful."
        )
        return 1
    print("OVERALL: PASS")
    print(
        "Omitted historical-tier periods carry no volume and emptying is emitted, "
        "so carrying the last quote forward reproduces the book rather than "
        "inventing one."
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
