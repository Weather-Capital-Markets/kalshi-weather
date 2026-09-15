"""Load-bearing K2 diagnostics that Session 6c did not measure.

Allowed DB: none. One candle load; snapshots at all census horizons.

Tests:
  1. NBM PIT / P10-P90 coverage of CLI high (and ASOS KNYC max)
  2. Isotonic (PAV) repair rate on decoded_v441 ladders
  3. Taker edge vs half-spread at T-24h (YES rich/cheap after touching the book)
  4. Strict-15 vs carry-forward Brier
  5. Quote-age split (stale-book artifact)
  6. Day-level sum of NBM probs and two-sided market mids
  7. Brier by horizon (T-36h … T-1h) on the same 300-day sample

Measurement only — no pass/fail verdict.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.bracket_enumeration import parse_market_strike
from analysis.forecast_vs_market import (
    bracket_probabilities_for_markets,
    cdf_at_temperature,
    load_nbm_ladder,
    markets_for_climate_date,
    sample_climate_dates,
    settled_yes,
)
from analysis.spread_census import (
    HORIZONS_H,
    build_snapshot_table,
    era_of,
    load_candles,
    load_markets,
    season_of,
    select_full_day_labels,
    ticker_climate_date,
)
from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import climate_date_of
from ingestion.climate_time import (
    asos_max_for_climate_day,
    cli_max_instant,
    load_cli_time_convention,
    nbm_max_window_utc,
    time_in_window,
)
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)

NOTE_TAKER = (
    "NOTE: taker_sell_yes_edge = bid - nbm_prob = (mid - half_spread) - nbm. "
    "Positive means YES is rich even after hitting the bid. Maker fee on KXHIGHNY "
    "was $0 as of 2026-08-15; taker fee is the default schedule (not re-checked here)."
)
NOTE_PIT = (
    "NOTE: P10-P90 of a calibrated max-window ladder should contain ~80% of outcomes "
    "of the *same* 12Z-06Z maximum. CLI high is a full climate-day max, so PIT vs CLI "
    "is biased by window mismatch; PIT vs ASOS KNYC daily max is the closer check "
    "and still not identical to the NBM 12Z-06Z product."
)
NOTE_HORIZON = (
    "NOTE: NBM vintage is D-1 12Z f042, a T-24h snapshot. Later horizons compare "
    "that same frozen forecast to a closer market, not a later NBM cycle."
)


def _finite(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if value is None:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def taker_edges(mid: float, nbm: float, spread: float) -> tuple[float, float]:
    """Return (taker_sell_yes_edge, taker_buy_yes_edge) after crossing half-spread.

    taker_sell_yes = bid - nbm = (mid - half_spread) - nbm
    taker_buy_yes  = nbm - ask = nbm - (mid + half_spread)
    Positive means the trade is still favorable after hitting the book.
    """
    half = spread / 2.0
    return (mid - half) - nbm, nbm - (mid + half)


def pit_row(ladder: pd.DataFrame, high_f: float | None) -> dict[str, Any]:
    ordered = ladder.sort_values("percentile_level")
    levels = [int(v) for v in ordered["percentile_level"].tolist()]
    values = [float(v) for v in ordered["value_f"].tolist()]
    by_level = {
        int(row.percentile_level): float(row.value_f) for row in ordered.itertuples(index=False)
    }
    p10 = by_level.get(10)
    p50 = by_level.get(50)
    p90 = by_level.get(90)
    repaired = False
    if "isotonic_adjusted" in ordered.columns:
        repaired = bool(ordered["isotonic_adjusted"].any())
    out: dict[str, Any] = {
        "nbm_p10_f": p10,
        "nbm_p50_f": p50,
        "nbm_p90_f": p90,
        "isotonic_repaired": repaired,
        "pit_cdf": None,
        "below_p10": None,
        "in_p10_p90": None,
        "above_p90": None,
        "high_minus_p50": None,
    }
    high = _finite(high_f)
    if high is None or p10 is None or p90 is None or p50 is None:
        return out
    out["pit_cdf"] = cdf_at_temperature(levels, values, high)
    out["below_p10"] = high < p10
    out["in_p10_p90"] = p10 <= high <= p90
    out["above_p90"] = high > p90
    out["high_minus_p50"] = high - p50
    return out


def brier_pair(
    frame: pd.DataFrame, prob_col: str, mid_col: str
) -> tuple[float | None, float | None]:
    settled = frame["settled_yes"].notna() & frame[prob_col].notna() & frame[mid_col].notna()
    if not settled.any():
        return None, None
    yes = frame.loc[settled, "settled_yes"].astype(float)
    nbm = float(((frame.loc[settled, prob_col] - yes) ** 2).mean())
    mkt = float(((frame.loc[settled, mid_col] - yes) ** 2).mean())
    return nbm, mkt


def _write_slice_tables(pit: pd.DataFrame, tradable: pd.DataFrame, out_dir: Path) -> None:
    pit_rows: list[dict[str, Any]] = []
    if not pit.empty and "season" in pit.columns:
        for season, group in pit.groupby("season"):
            pit_rows.append(
                {
                    "slice": f"cli_{season}",
                    "n": int(len(group)),
                    "below_p10": float(group["cli_below_p10"].mean()),
                    "in_p10_p90": float(group["cli_in_p10_p90"].mean()),
                    "above_p90": float(group["cli_above_p90"].mean()),
                    "mae_p50": float(group["cli_high_minus_p50"].abs().mean()),
                    "bias_high_minus_p50": float(group["cli_high_minus_p50"].mean()),
                }
            )
            pit_rows.append(
                {
                    "slice": f"asos_{season}",
                    "n": int(len(group)),
                    "below_p10": float(group["asos_below_p10"].mean()),
                    "in_p10_p90": float(group["asos_in_p10_p90"].mean()),
                    "above_p90": float(group["asos_above_p90"].mean()),
                    "mae_p50": float(group["asos_high_minus_p50"].abs().mean()),
                    "bias_high_minus_p50": float(group["asos_high_minus_p50"].mean()),
                }
            )
    pd.DataFrame(pit_rows).to_csv(out_dir / "k2_pit_by_season.csv", index=False)

    taker_rows: list[dict[str, Any]] = []

    def _taker_slice(name: str, group: pd.DataFrame) -> dict[str, Any]:
        return {
            "slice": name,
            "n": int(len(group)),
            "median_spread": float(group["spread_carryforward"].median()),
            "median_edge_cf": float(group["edge_cf"].median()),
            "median_taker_sell_yes": float(group["taker_sell_yes_edge"].median()),
            "frac_taker_sell_gt_0": float((group["taker_sell_yes_edge"] > 0).mean()),
            "frac_taker_sell_gt_4c": float((group["taker_sell_yes_edge"] > 0.04).mean()),
            "frac_taker_buy_gt_0": float((group["taker_buy_yes_edge"] > 0).mean()),
        }

    if not tradable.empty:
        for season, group in tradable.groupby("season"):
            taker_rows.append(_taker_slice(str(season), group))
        books = tradable.copy()
        books["book"] = (
            books["quote_age_sec"]
            .le(15 * 60)
            .map({True: "fresh_le_15min", False: "stale_gt_15min"})
        )
        for book, group in books.groupby("book"):
            taker_rows.append(_taker_slice(str(book), group))
        if "strike_role" in tradable.columns:
            for role, group in tradable.groupby("strike_role"):
                taker_rows.append(_taker_slice(f"role_{role}", group))
    pd.DataFrame(taker_rows).to_csv(out_dir / "k2_taker_slices.csv", index=False)


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config.get("storage") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    fv = config.get("forecast_vs_market") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    decoded_dir = Path(nbm_cfg.get("decoded_dir") or "data/nbm/decoded_v441")
    band = fv.get("primary_band") or [0.10, 0.90]
    band_lo, band_hi = float(band[0]), float(band[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    climate_dates = sample_climate_dates(config)
    climate_set = set(climate_dates)
    print(f"sample_days={len(climate_dates)}")

    all_markets = load_markets(raw_dir)
    markets = [
        market
        for market in all_markets
        if ticker_climate_date(str(market.get("ticker") or "")) in climate_set
    ]
    candles = load_candles(raw_dir)
    labels = select_full_day_labels(labels_csv)
    cli_convention = load_cli_time_convention(config)
    snapshots, _stats = build_snapshot_table(
        markets=markets,
        candles_by_ticker=candles,
        labels=labels,
        cli_time_convention=cli_convention,
    )
    print(f"snapshot_rows={len(snapshots)} markets={len(markets)}")

    label_map = {}
    if not labels.empty:
        for row in labels.itertuples(index=False):
            label_map[str(row.climate_date)] = row
    asos_obs = load_asos_observations_from_raw(raw_dir, station="NYC")
    asos_by_day: dict[str, list] = {}
    for obs in asos_obs:
        asos_by_day.setdefault(climate_date_of(obs.valid_utc).isoformat(), []).append(obs)

    pit_rows: list[dict[str, Any]] = []
    probs_by_day: dict[str, dict[str, float]] = {}
    for climate_date in climate_dates:
        ladder = load_nbm_ladder(decoded_dir, climate_date)
        day_markets = markets_for_climate_date(markets, climate_date)
        if ladder is None or not day_markets:
            continue
        probs_by_day[climate_date] = bracket_probabilities_for_markets(ladder, day_markets)
        day_snaps = snapshots[snapshots["climate_date"] == climate_date]
        cli_high = None
        if not day_snaps.empty:
            cli_high = _finite(day_snaps.iloc[0].get("high_F"))
        asos_max_f, _ts = asos_max_for_climate_day(asos_by_day.get(climate_date, []), climate_date)
        cli_pit = pit_row(ladder, cli_high)
        asos_pit = pit_row(ladder, asos_max_f)
        outside_lst = None
        label = label_map.get(climate_date)
        if label is not None:
            time_raw = str(getattr(label, "time_of_high_raw", "") or "")
            if time_raw.strip():
                window_start, window_end = nbm_max_window_utc(climate_date)
                instant = cli_max_instant(climate_date, time_raw, "lst")
                if instant is not None:
                    outside_lst = not time_in_window(instant, window_start, window_end)
        pit_rows.append(
            {
                "climate_date": climate_date,
                "season": season_of(climate_date),
                "cli_high_f": cli_high,
                "asos_max_f": asos_max_f,
                "cli_outside_lst": outside_lst,
                "nbm_prob_sum": float(sum(probs_by_day[climate_date].values())),
                **{f"cli_{k}": v for k, v in cli_pit.items()},
                **{f"asos_{k}": v for k, v in asos_pit.items()},
            }
        )
    pit = pd.DataFrame(pit_rows)
    pit_path = out_dir / "k2_pit_days.csv"
    pit.to_csv(pit_path, index=False)
    print(f"wrote {pit_path} rows={len(pit)}")

    def _rate(series: pd.Series) -> float | None:
        clean = series.dropna()
        if clean.empty:
            return None
        return float(clean.mean())

    print("\n=== NBM PIT vs CLI high ===")
    print(
        f"days={len(pit)} isotonic_repair_rate={_rate(pit['cli_isotonic_repaired'])} "
        f"below_p10={_rate(pit['cli_below_p10'])} in_p10_p90={_rate(pit['cli_in_p10_p90'])} "
        f"above_p90={_rate(pit['cli_above_p90'])} "
        f"mae_p50={pit['cli_high_minus_p50'].abs().mean():.3f}"
    )
    print("=== NBM PIT vs ASOS KNYC max ===")
    print(
        f"below_p10={_rate(pit['asos_below_p10'])} in_p10_p90={_rate(pit['asos_in_p10_p90'])} "
        f"above_p90={_rate(pit['asos_above_p90'])} "
        f"mae_p50={pit['asos_high_minus_p50'].abs().mean():.3f}"
    )
    if pit["cli_outside_lst"].notna().any():
        inside = pit[pit["cli_outside_lst"] == False]  # noqa: E712
        outside = pit[pit["cli_outside_lst"] == True]  # noqa: E712
        print(
            f"CLI in-window in_p10_p90={_rate(inside['cli_in_p10_p90'])} "
            f"n={len(inside)}; outside in_p10_p90="
            f"{_rate(outside['cli_in_p10_p90'])} n={len(outside)}"
        )

    book_rows: list[dict[str, Any]] = []
    for climate_date, probs in probs_by_day.items():
        day_markets = {
            str(m.get("ticker") or ""): m for m in markets_for_climate_date(markets, climate_date)
        }
        day_snaps = snapshots[snapshots["climate_date"] == climate_date]
        for _, snap in day_snaps.iterrows():
            ticker = str(snap["ticker"])
            if ticker not in probs:
                continue
            market = day_markets.get(ticker)
            strike = parse_market_strike(market) if market else None
            nbm = probs[ticker]
            mid_cf = _finite(snap.get("mid_carryforward"))
            spread_cf = _finite(snap.get("spread_carryforward"))
            mid_s15 = _finite(snap.get("mid"))
            high_f = _finite(snap.get("high_F"))
            sell_edge = buy_edge = None
            half = spread_cf / 2.0 if spread_cf is not None else None
            if mid_cf is not None and spread_cf is not None:
                sell_edge, buy_edge = taker_edges(mid_cf, nbm, spread_cf)
            in_band = (
                bool(snap["two_sided_carryforward"])
                and mid_cf is not None
                and band_lo <= mid_cf <= band_hi
            )
            book_rows.append(
                {
                    "climate_date": climate_date,
                    "season": season_of(climate_date),
                    "era": era_of(climate_date),
                    "horizon_h": int(snap["horizon_h"]),
                    "ticker": ticker,
                    "strike_role": strike.role if strike else None,
                    "nbm_prob": nbm,
                    "market_mid_carryforward": mid_cf,
                    "spread_carryforward": spread_cf,
                    "half_spread": half,
                    "edge_cf": (mid_cf - nbm) if mid_cf is not None else None,
                    "taker_sell_yes_edge": sell_edge,
                    "taker_buy_yes_edge": buy_edge,
                    "two_sided_carryforward": bool(snap["two_sided_carryforward"]),
                    "two_sided_strict15": bool(snap["two_sided"]),
                    "mid_strict15": mid_s15,
                    "in_primary_band": in_band,
                    "in_trading_window": bool(snap["in_trading_window"]),
                    "quote_age_sec": _finite(snap.get("quote_age_sec")),
                    "settled_yes": settled_yes(high_f, strike) if strike else None,
                }
            )
    books = pd.DataFrame(book_rows)
    books_path = out_dir / "k2_horizon_books.csv"
    books.to_csv(books_path, index=False)
    print(f"wrote {books_path} rows={len(books)}")

    t24 = books[(books["horizon_h"] == 24) & books["in_primary_band"]].copy()
    tradable = t24.dropna(subset=["taker_sell_yes_edge", "taker_buy_yes_edge", "half_spread"])
    print("\n=== Taker edge vs half-spread at T-24h (primary band) ===")
    print(
        f"n={len(tradable)} median_spread={tradable['spread_carryforward'].median():.4f} "
        f"median_abs_edge={tradable['edge_cf'].abs().median():.4f}"
    )
    print(
        f"frac_|edge|>half_spread="
        f"{float((tradable['edge_cf'].abs() > tradable['half_spread']).mean()):.3f} "
        f"frac_taker_sell_yes>0={float((tradable['taker_sell_yes_edge'] > 0).mean()):.3f} "
        f"median_taker_sell_yes={tradable['taker_sell_yes_edge'].median():.4f} "
        f"frac_taker_buy_yes>0={float((tradable['taker_buy_yes_edge'] > 0).mean()):.3f} "
        f"median_taker_buy_yes={tradable['taker_buy_yes_edge'].median():.4f}"
    )
    print(
        f"frac_taker_sell_yes>2c={float((tradable['taker_sell_yes_edge'] > 0.02).mean()):.3f} "
        f"frac_taker_sell_yes>4c={float((tradable['taker_sell_yes_edge'] > 0.04).mean()):.3f}"
    )

    nbm_cf, mkt_cf = brier_pair(t24, "nbm_prob", "market_mid_carryforward")
    s15 = t24[t24["two_sided_strict15"] & t24["mid_strict15"].notna()].copy()
    nbm_s15, mkt_s15 = brier_pair(s15, "nbm_prob", "mid_strict15")
    print("\n=== Carry-forward vs strict-15 Brier (T-24h primary) ===")
    print(f"cf n={len(t24)} nbm={nbm_cf} market={mkt_cf}")
    print(f"s15 n={len(s15)} nbm={nbm_s15} market={mkt_s15}")

    fresh = t24[t24["quote_age_sec"].notna() & (t24["quote_age_sec"] <= 15 * 60)]
    stale = t24[t24["quote_age_sec"].notna() & (t24["quote_age_sec"] > 15 * 60)]
    print("\n=== Quote age at T-24h (primary, carry-forward mids) ===")
    print(
        f"median_age_min={t24['quote_age_sec'].median() / 60.0:.1f} "
        f"p90_age_min={t24['quote_age_sec'].quantile(0.90) / 60.0:.1f} "
        f"frac>15min={float((t24['quote_age_sec'] > 15 * 60).mean()):.3f}"
    )
    nbm_f, mkt_f = brier_pair(fresh, "nbm_prob", "market_mid_carryforward")
    nbm_k, mkt_k = brier_pair(stale, "nbm_prob", "market_mid_carryforward")
    print(f"age<=15min n={len(fresh)} nbm={nbm_f} market={mkt_f}")
    print(f"age>15min n={len(stale)} nbm={nbm_k} market={mkt_k}")

    t24_all = books[books["horizon_h"] == 24]
    day_sum_rows: list[dict[str, Any]] = []
    for climate_date, group in t24_all.groupby("climate_date"):
        two = group[group["two_sided_carryforward"] & group["market_mid_carryforward"].notna()]
        day_sum_rows.append(
            {
                "climate_date": climate_date,
                "n_brackets": int(len(group.drop_duplicates("ticker"))),
                "n_two_sided": int(len(two)),
                "nbm_prob_sum": float(group.drop_duplicates("ticker")["nbm_prob"].sum()),
                "market_mid_sum_two_sided": (
                    float(two["market_mid_carryforward"].sum()) if len(two) else None
                ),
            }
        )
    day_sums = pd.DataFrame(day_sum_rows)
    day_path = out_dir / "k2_day_sum_law.csv"
    day_sums.to_csv(day_path, index=False)
    print("\n=== Day-level sum law (T-24h) ===")
    all_two_sided = float((day_sums["n_two_sided"] == day_sums["n_brackets"]).mean())
    print(
        f"median_nbm_sum={day_sums['nbm_prob_sum'].median():.4f} "
        f"median_market_two_sided_sum={day_sums['market_mid_sum_two_sided'].median():.4f} "
        f"frac_all_brackets_two_sided={all_two_sided:.3f}"
    )

    horizon_rows: list[dict[str, Any]] = []
    for horizon in HORIZONS_H:
        subset = books[(books["horizon_h"] == horizon) & books["in_primary_band"]].copy()
        nbm_b, mkt_b = brier_pair(subset, "nbm_prob", "market_mid_carryforward")
        edge = subset["edge_cf"].dropna()
        trad = subset.dropna(subset=["taker_sell_yes_edge", "half_spread"])
        horizon_rows.append(
            {
                "horizon_h": horizon,
                "n_obs": int(len(subset)),
                "n_days": int(subset["climate_date"].nunique()) if len(subset) else 0,
                "nbm_brier": nbm_b,
                "market_brier": mkt_b,
                "brier_gap": (mkt_b - nbm_b) if nbm_b is not None and mkt_b is not None else None,
                "edge_cf_median": float(edge.median()) if len(edge) else None,
                "median_spread": (
                    float(subset["spread_carryforward"].median()) if len(subset) else None
                ),
                "frac_taker_sell_yes>0": (
                    float((trad["taker_sell_yes_edge"] > 0).mean()) if len(trad) else None
                ),
                "median_taker_sell_yes": (
                    float(trad["taker_sell_yes_edge"].median()) if len(trad) else None
                ),
            }
        )
    horizons = pd.DataFrame(horizon_rows)
    h_path = out_dir / "k2_brier_by_horizon.csv"
    horizons.to_csv(h_path, index=False)
    print("\n=== Brier by horizon (frozen T-24h NBM vs closer market) ===")
    print(horizons.to_string(index=False))

    trad_path = out_dir / "k2_t24_tradable.csv"
    tradable.to_csv(trad_path, index=False)
    print(f"\nwrote {trad_path}")

    summary = pd.DataFrame(
        [
            {"metric": "pit_days", "value": float(len(pit)), "n": int(len(pit))},
            {
                "metric": "isotonic_repair_rate",
                "value": _rate(pit["cli_isotonic_repaired"]),
                "n": int(len(pit)),
            },
            {
                "metric": "cli_below_p10",
                "value": _rate(pit["cli_below_p10"]),
                "n": int(pit["cli_below_p10"].notna().sum()),
            },
            {
                "metric": "cli_in_p10_p90",
                "value": _rate(pit["cli_in_p10_p90"]),
                "n": int(pit["cli_in_p10_p90"].notna().sum()),
            },
            {
                "metric": "cli_above_p90",
                "value": _rate(pit["cli_above_p90"]),
                "n": int(pit["cli_above_p90"].notna().sum()),
            },
            {
                "metric": "asos_in_p10_p90",
                "value": _rate(pit["asos_in_p10_p90"]),
                "n": int(pit["asos_in_p10_p90"].notna().sum()),
            },
            {
                "metric": "t24_primary_n",
                "value": float(len(tradable)),
                "n": int(len(tradable)),
            },
            {
                "metric": "median_spread",
                "value": float(tradable["spread_carryforward"].median()) if len(tradable) else None,
                "n": int(len(tradable)),
            },
            {
                "metric": "frac_abs_edge_gt_half_spread",
                "value": (
                    float((tradable["edge_cf"].abs() > tradable["half_spread"]).mean())
                    if len(tradable)
                    else None
                ),
                "n": int(len(tradable)),
            },
            {
                "metric": "frac_taker_sell_yes_gt_0",
                "value": (
                    float((tradable["taker_sell_yes_edge"] > 0).mean()) if len(tradable) else None
                ),
                "n": int(len(tradable)),
            },
            {
                "metric": "median_taker_sell_yes",
                "value": float(tradable["taker_sell_yes_edge"].median()) if len(tradable) else None,
                "n": int(len(tradable)),
            },
            {
                "metric": "frac_taker_buy_yes_gt_0",
                "value": (
                    float((tradable["taker_buy_yes_edge"] > 0).mean()) if len(tradable) else None
                ),
                "n": int(len(tradable)),
            },
            {"metric": "nbm_brier_carryforward", "value": nbm_cf, "n": int(len(t24))},
            {"metric": "market_brier_carryforward", "value": mkt_cf, "n": int(len(t24))},
            {"metric": "nbm_brier_strict15", "value": nbm_s15, "n": int(len(s15))},
            {"metric": "market_brier_strict15", "value": mkt_s15, "n": int(len(s15))},
            {
                "metric": "median_quote_age_min",
                "value": (
                    float(t24["quote_age_sec"].median() / 60.0)
                    if t24["quote_age_sec"].notna().any()
                    else None
                ),
                "n": int(t24["quote_age_sec"].notna().sum()),
            },
            {
                "metric": "frac_quote_age_gt_15min",
                "value": float((t24["quote_age_sec"] > 15 * 60).mean()) if len(t24) else None,
                "n": int(len(t24)),
            },
            {
                "metric": "median_nbm_prob_sum",
                "value": float(day_sums["nbm_prob_sum"].median()) if len(day_sums) else None,
                "n": int(len(day_sums)),
            },
            {
                "metric": "median_market_two_sided_sum",
                "value": (
                    float(day_sums["market_mid_sum_two_sided"].median()) if len(day_sums) else None
                ),
                "n": int(len(day_sums)),
            },
        ]
    )
    summary_path = out_dir / "k2_diagnostics_summary.csv"
    summary.to_csv(summary_path, index=False)
    print(f"wrote {summary_path}")
    _write_slice_tables(pit, tradable, out_dir)
    print(NOTE_TAKER)
    print(NOTE_PIT)
    print(NOTE_HORIZON)
    return 0 if len(pit) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K2 diagnostics not covered by Session 6c")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    cfg = config.get("forecast_vs_market") or {}
    out_dir = args.out_dir or Path(cfg.get("out_dir") or "analysis/out")
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
