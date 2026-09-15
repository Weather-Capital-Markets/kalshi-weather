"""Enumerate Kalshi KXHIGHNY bracket structure by era from frozen market metadata.

Allowed DB: none. Reads markets_history JSONL only; no network.

Resolves the blocking K2 prerequisite: verify bracket width, boundary alignment,
contiguity, and tail structure from floor_strike / cap_strike / strike_type rather
than assuming 2°F bins from the working record.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.spread_census import load_markets, ticker_climate_date
from analysis.venue_eras import change_points
from ingestion.config_loader import load_config
from ingestion.validate_units import assert_non_empty_frame
from wxmm.settlement.brackets import (
    SUBTITLE_KEYS,
    ParsedStrike,
    parse_market_strike,
)

logger = logging.getLogger(__name__)

TICKER_SUFFIX = re.compile(r"-([TB][A-Z0-9.]+)$", re.IGNORECASE)


def _label_text(market: dict[str, Any]) -> str:
    for key in SUBTITLE_KEYS:
        raw = market.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def classify_ticker_suffix(ticker: str) -> str:
    match = TICKER_SUFFIX.search(ticker)
    if not match:
        return "other"
    suffix = match.group(1).upper()
    if suffix.startswith("T"):
        return "T_tail"
    if suffix.startswith("B") and suffix.endswith(".5"):
        return "B_midpoint"
    if suffix.startswith("B"):
        return "B_other"
    return "other"


def interior_bins(strikes: list[ParsedStrike]) -> list[tuple[int, int]]:
    bins: list[tuple[int, int]] = []
    for strike in strikes:
        if strike.role != "between" or strike.floor_f is None or strike.cap_f is None:
            continue
        bins.append((strike.floor_f, strike.cap_f))
    return sorted(bins)


def contiguous_interior(bins: list[tuple[int, int]]) -> bool:
    if len(bins) <= 1:
        return True
    for idx in range(1, len(bins)):
        prev_low, prev_high = bins[idx - 1]
        low, _high = bins[idx]
        if low != prev_high + 1:
            return False
    return True


def boundary_alignment_label(bins: list[tuple[int, int]], *, width: int | None) -> str:
    if not bins:
        return "no_interior"
    if width != 2:
        return f"width_{width}f" if width is not None else "mixed_width"
    floors = [low for low, _high in bins]
    caps = [high for _low, high in bins]
    parity = "even" if all(v % 2 == 0 for v in floors + caps) else "mixed_parity"
    if all(high - low + 1 == 2 for low, high in bins):
        return f"integer_2f_bins_{parity}"
    return "nonuniform_interior"


def suffix_scheme(tickers: list[str]) -> str:
    classes = {classify_ticker_suffix(ticker) for ticker in tickers}
    if classes <= {"T_tail", "B_midpoint"}:
        return "T_tail_B_midpoint"
    if classes <= {"T_tail", "B_other", "B_midpoint"}:
        return "T_tail_B_mixed"
    if classes == {"T_tail"}:
        return "T_only"
    return "+".join(sorted(classes))


def event_structure(
    climate_date: str,
    markets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not markets:
        return None
    tickers = [str(m.get("ticker") or "") for m in markets if m.get("ticker")]
    parsed: list[ParsedStrike] = []
    unparsed = 0
    for market in markets:
        strike = parse_market_strike(market)
        if strike is None:
            unparsed += 1
        else:
            parsed.append(strike)

    interior = [s for s in parsed if s.role == "between"]
    tails = [s for s in parsed if s.role in {"greater", "less"}]
    bins = interior_bins(parsed)
    widths = [s.width_f for s in interior if s.width_f is not None]
    width_counts = Counter(widths)
    if not width_counts:
        modal_width: int | None = None
        mixed_widths = False
    else:
        modal_width = width_counts.most_common(1)[0][0]
        mixed_widths = len(width_counts) > 1

    has_less = any(s.role == "less" for s in parsed)
    has_greater = any(s.role == "greater" for s in parsed)
    alignment = boundary_alignment_label(bins, width=modal_width)
    scheme = suffix_scheme(tickers)
    structure_id = (
        f"n={len(markets)}",
        f"width={modal_width}",
        f"align={alignment}",
        f"contig={contiguous_interior(bins)}",
        f"tails={has_less and has_greater}",
        f"suffix={scheme}",
    )

    return {
        "climate_date": climate_date,
        "n_brackets": len(markets),
        "n_parsed": len(parsed),
        "n_unparsed": unparsed,
        "n_interior": len(interior),
        "n_tails": len(tails),
        "interior_width_f": modal_width,
        "mixed_interior_widths": mixed_widths,
        "boundary_alignment": alignment,
        "contiguous_interior": contiguous_interior(bins),
        "open_tails": has_less and has_greater,
        "suffix_scheme": scheme,
        "structure_id": "|".join(structure_id),
    }


def build_daily_table(
    markets: list[dict[str, Any]],
    *,
    start_date: date,
) -> pd.DataFrame:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        climate_date = ticker_climate_date(str(market.get("ticker") or ""))
        if climate_date is None:
            continue
        if date.fromisoformat(climate_date) < start_date:
            continue
        by_day.setdefault(climate_date, []).append(market)

    rows: list[dict[str, Any]] = []
    for climate_date in sorted(by_day):
        row = event_structure(climate_date, by_day[climate_date])
        if row is not None:
            rows.append(row)
    return pd.DataFrame(rows)


def daily_modal_structure(daily: pd.DataFrame) -> pd.Series:
    if daily.empty:
        return pd.Series(dtype=str)
    return (
        daily.groupby("climate_date")["structure_id"]
        .agg(lambda values: Counter(values).most_common(1)[0][0])
        .sort_index()
    )


def build_regime_table(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    modal = daily_modal_structure(daily)
    points = change_points(modal)
    regimes: list[dict[str, Any]] = []
    if modal.empty:
        return pd.DataFrame(regimes)

    boundaries = [0] + [modal.index.get_loc(p["first_date_after"]) for p in points]
    boundaries.append(len(modal))
    structure_values = list(modal.values)
    structure_dates = list(modal.index)

    for idx in range(len(boundaries) - 1):
        start_i = boundaries[idx]
        end_i = boundaries[idx + 1] - 1
        structure_id = structure_values[start_i]
        first_date = structure_dates[start_i]
        last_date = structure_dates[end_i]
        subset = daily[daily["climate_date"].between(first_date, last_date)]
        example = subset.iloc[0]
        median_brackets = (
            float(subset["n_brackets"].median()) if "n_brackets" in subset.columns else float("nan")
        )
        regimes.append(
            {
                "structure_id": structure_id,
                "first_date": first_date,
                "last_date": last_date,
                "n_climate_days": int(end_i - start_i + 1),
                "median_brackets_per_day": median_brackets,
                "interior_width_f": example.get("interior_width_f"),
                "boundary_alignment": example.get("boundary_alignment"),
                "contiguous_interior": bool(example.get("contiguous_interior")),
                "open_tails": bool(example.get("open_tails")),
                "suffix_scheme": example.get("suffix_scheme"),
            }
        )
    return pd.DataFrame(regimes)


def verdict_on_two_degree_hypothesis(regimes: pd.DataFrame, daily: pd.DataFrame) -> str:
    if regimes.empty or daily.empty:
        return "insufficient_data"
    width_days = daily.dropna(subset=["interior_width_f"])
    if width_days.empty:
        return "no_interior_width_observed"
    non_two = width_days[width_days["interior_width_f"] != 2]
    if non_two.empty and bool((regimes["interior_width_f"] == 2).all()):
        return "confirmed_2f_bins"
    if len(regimes) == 1 and regimes.iloc[0]["interior_width_f"] == 2:
        return "confirmed_2f_bins"
    if len(non_two) > 0 and len(regimes) > 1:
        return "era_dependent"
    if len(non_two) > 0:
        return "falsified_or_sparse"
    return "era_dependent"


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    cfg = config.get("bracket_enumeration") or {}
    start_date = date.fromisoformat(str(cfg.get("start_date") or "2021-08-05"))

    markets = load_markets(raw_dir)
    if not markets:
        print("no markets_history data in raw_dir")
        return 1

    daily = build_daily_table(markets, start_date=start_date)
    if daily.empty:
        print("no parseable climate days in span")
        return 1

    regimes = build_regime_table(daily)
    verdict = verdict_on_two_degree_hypothesis(regimes, daily)

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "bracket_structure.csv"
    assert_non_empty_frame(regimes, what="bracket_structure.csv")
    regimes.to_csv(csv_path, index=False)

    print(f"climate_days={len(daily)} markets={len(markets)} start={start_date.isoformat()}")
    print("\n=== bracket structure regimes ===")
    print(regimes.to_string(index=False))
    print("\n=== regime changeovers ===")
    modal = daily_modal_structure(daily)
    for point in change_points(modal):
        print(
            f"changeover between {point['last_date_before']} and {point['first_date_after']}: "
            f"{point['value_before']} -> {point['value_after']} "
            f"({point['days_before']} days before, {point['days_after']} after)"
        )
    if not change_points(modal):
        print("no changeover detected; one structure holds across the capture")

    print(f"\n=== 2°F bin hypothesis verdict: {verdict} ===")
    print(f"wrote {csv_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi bracket structure by era")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    default_out = (config.get("bracket_enumeration") or {}).get("out_dir") or "analysis/out"
    out_dir = args.out_dir or Path(default_out)
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
