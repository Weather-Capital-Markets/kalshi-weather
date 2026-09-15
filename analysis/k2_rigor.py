"""Rigorous K2 follow-ups that do not require a new NBM vintage.

Allowed DB: none. Reads existing CSVs, decoded_v441 parquet, CLINYC, ASOS,
logger orderbooks/trades, and historical candlesticks.

Covers:
  1. PIT vs ASOS max inside the NBM 12Z-06Z window (true product)
  2. Murphy Brier decomposition (reliability vs resolution)
  3. Taker-sell by NBM tercile and by ladder width (P90-P10)
  4. CLI prelim vs final at era-correct settlement snapshot
  5. Quadratic taker-fee haircut at the touch price
  6. Offline emission: logger books vs on-disk candlesticks
  7. Trades vs contemporaneous top of book on logger days

Measurement only. Later-cycle NBM and 99-level extracts are separate
nbm_archive runs into new directories.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.k2_diagnostics import pit_row
from analysis.murphy import murphy_model_rows
from analysis.spread_census import (
    candle_fields,
    parse_iso_utc,
    season_of,
)
from analysis.validate_emission_forward import (
    PRICE_TOLERANCE,
    compare_ticker,
    discover_tickers,
    load_logger_books,
    minute_boundaries,
)
from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import NY_CIVIL, climate_date_of
from ingestion.climate_time import asos_max_in_window, nbm_max_window_utc
from ingestion.config_loader import load_config
from ingestion.writer import read_jsonl_gz

logger = logging.getLogger(__name__)

ERA_10AM_LAST = date(2024, 9, 3)
ERA_UNSPECIFIED_LAST = date(2021, 12, 25)
TAKER_COEFF = 0.07
NOTE_FEE = (
    "NOTE: taker fee is ceil_6dp(0.07 * P * (1-P)) per contract (C=1, M=1). "
    "Kalshi API FeeType=quadratic; 0.07 is the published general schedule. "
    "KXHIGHNY maker $0 verified 2026-08-15 (venue-facts §2.1). Applied at the "
    "touch price. Re-check the live fee page before capital."
)
NOTE_WINDOW = (
    "NOTE: asos_window is hourly KNYC max inside 12Z-06Z, the NBM product. "
    "CLI high remains a full climate-day max."
)
NOTE_LABEL = (
    "NOTE: 2021 unspecified era (through 2021-12-25) is tagged assume_10am, not "
    "a ratified Rule 100.19 reading. 7/8 AM era is reported at both 07:00 and "
    "08:00 ET on climate_date+1."
)

LOGGER_DAYS = ("2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-20")


def ceil_6dp(value: float) -> float:
    return math.ceil(value * 1_000_000.0 - 1e-12) / 1_000_000.0


def quadratic_taker_fee(price: float, *, coeff: float = TAKER_COEFF) -> float:
    """Per-contract quadratic taker fee at a dollar price in (0, 1)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return ceil_6dp(coeff * price * (1.0 - price))


def settlement_era(climate_date: str) -> str:
    day = date.fromisoformat(climate_date)
    if day <= ERA_UNSPECIFIED_LAST:
        return "unspecified_assume_10am"
    if day <= ERA_10AM_LAST:
        return "first_10am"
    return "first_7_or_8am"


def settlement_snapshot_utc(climate_date: str, hour_et: int) -> datetime:
    """Morning-after snapshot on climate_date+1 at hour_et in NY civil time."""
    day = date.fromisoformat(climate_date) + timedelta(days=1)
    naive = datetime(day.year, day.month, day.day, hour_et, 0)
    return naive.replace(tzinfo=NY_CIVIL).astimezone(timezone.utc)


def _is_intermediate(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _parse_high(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    text = str(value).strip()
    if not text or text.upper() == "MM" or text.lower() == "nan":
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def cli_revision_table(labels: pd.DataFrame) -> pd.DataFrame:
    """Per climate-day: label visible at settlement snapshot vs last full-day high."""
    if labels.empty:
        return pd.DataFrame()
    work = labels.copy()
    work["climate_date"] = work["climate_date"].astype(str)
    work["issuance_dt"] = pd.to_datetime(work["issuance_ts_utc"], utc=True, errors="coerce")
    work["intermediate"] = work["is_same_day_intermediate"].map(_is_intermediate)
    full = work.loc[~work["intermediate"]].copy()
    rows: list[dict[str, Any]] = []
    for climate_date, group in full.groupby("climate_date"):
        ordered = group.sort_values("issuance_dt")
        last = ordered.iloc[-1]
        last_high = _parse_high(last.get("high_F"))
        era = settlement_era(str(climate_date))
        hours = (10,) if era != "first_7_or_8am" else (7, 8)
        record: dict[str, Any] = {
            "climate_date": climate_date,
            "season": season_of(str(climate_date)),
            "era": era,
            "n_full_issuances": int(len(ordered)),
            "last_high_f": last_high,
            "last_issuance_ts": (
                last["issuance_dt"].isoformat() if pd.notna(last["issuance_dt"]) else None
            ),
        }
        for hour in hours:
            snapshot = settlement_snapshot_utc(str(climate_date), hour)
            visible = ordered[ordered["issuance_dt"].notna() & (ordered["issuance_dt"] <= snapshot)]
            if visible.empty:
                record[f"snap_{hour:02d}_high_f"] = None
                record[f"snap_{hour:02d}_late"] = True
                record[f"snap_{hour:02d}_disagree"] = None
            else:
                chosen = visible.iloc[-1]
                snap_high = _parse_high(chosen.get("high_F"))
                record[f"snap_{hour:02d}_high_f"] = snap_high
                record[f"snap_{hour:02d}_late"] = False
                record[f"snap_{hour:02d}_disagree"] = (
                    snap_high != last_high
                    if snap_high is not None and last_high is not None
                    else None
                )
        rows.append(record)
    return pd.DataFrame(rows)


def load_nbm_ladder(decoded_dir: Path, climate_date: str) -> pd.DataFrame | None:
    path = decoded_dir / f"{climate_date}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def window_pit_table(
    *,
    pit: pd.DataFrame,
    decoded_dir: Path,
    asos_by_day: dict[str, list],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in pit.itertuples(index=False):
        climate_date = str(row.climate_date)
        ladder = load_nbm_ladder(decoded_dir, climate_date)
        if ladder is None:
            continue
        window_start, window_end = nbm_max_window_utc(climate_date)
        win_max, win_ts = asos_max_in_window(
            asos_by_day.get(climate_date, []), window_start, window_end
        )
        win_pit = pit_row(ladder, win_max)
        rows.append(
            {
                "climate_date": climate_date,
                "season": row.season,
                "asos_window_max_f": win_max,
                "asos_window_max_utc": win_ts.isoformat() if win_ts is not None else None,
                **{f"win_{k}": v for k, v in win_pit.items()},
            }
        )
    return pd.DataFrame(rows)


def _qcut_tercile(series: pd.Series) -> pd.Series:
    return pd.qcut(series, 3, labels=["low", "mid", "high"], duplicates="drop").astype(str)


def _trade_price_dollars(raw: Any) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    if number > 1.0:
        number = number / 100.0
    if number < 0.0 or number > 1.0:
        return None
    return number


def load_candles_for_tickers(raw_dir: Path, tickers: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Load candlesticks only for named tickers (filename = ticker.jsonl.gz)."""
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for ticker in tickers:
        for path in raw_dir.glob(f"*/candlesticks/{ticker}.jsonl.gz"):
            for record in read_jsonl_gz(path):
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                candles = payload.get("candlesticks")
                if not isinstance(candles, list):
                    continue
                bucket = by_ticker.setdefault(ticker, [])
                for candle in candles:
                    if isinstance(candle, dict):
                        bucket.append(candle_fields(candle))
    return by_ticker


def load_logger_trades(raw_dir: Path, days: list[str]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for day in days:
        folder = raw_dir / day / "trades"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.jsonl.gz")):
            for record in read_jsonl_gz(path):
                payload = record.get("payload") or {}
                items = payload.get("trades") if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    continue
                for trade in items:
                    if not isinstance(trade, dict):
                        continue
                    created = parse_iso_utc(str(trade.get("created_time") or ""))
                    ticker = str(trade.get("ticker") or "")
                    price = _trade_price_dollars(
                        trade.get("yes_price_dollars")
                        or trade.get("yes_price")
                        or trade.get("price")
                    )
                    if created is None or not ticker or price is None:
                        continue
                    count = trade.get("count", trade.get("count_fp", 1))
                    try:
                        size = float(count)
                    except (TypeError, ValueError):
                        size = 1.0
                    trades.append(
                        {
                            "ticker": ticker,
                            "ts_utc": created,
                            "price": price,
                            "count": size,
                            "taker_side": trade.get("taker_side"),
                        }
                    )
    return trades


def classify_print(price: float, bid: float | None, ask: float | None) -> str:
    if bid is None or ask is None:
        return "no_book"
    if abs(price - bid) <= PRICE_TOLERANCE:
        return "at_bid"
    if abs(price - ask) <= PRICE_TOLERANCE:
        return "at_ask"
    if bid < price < ask:
        return "inside"
    return "outside"


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config.get("storage") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    raw_dir = Path(storage.get("raw_dir") or "data/raw")
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    decoded_dir = Path(nbm_cfg.get("decoded_dir") or "data/nbm/decoded_v441")
    out_dir.mkdir(parents=True, exist_ok=True)

    pit_path = out_dir / "k2_pit_days.csv"
    trad_path = out_dir / "k2_t24_tradable.csv"
    if not pit_path.exists() or not trad_path.exists():
        print("missing k2_pit_days.csv or k2_t24_tradable.csv — run analysis.k2_diagnostics first")
        return 1

    pit = pd.read_csv(pit_path)
    tradable = pd.read_csv(trad_path)
    print(f"pit_days={len(pit)} tradable={len(tradable)}")

    asos_obs = load_asos_observations_from_raw(raw_dir, station="NYC")
    asos_by_day: dict[str, list] = {}
    for obs in asos_obs:
        asos_by_day.setdefault(climate_date_of(obs.valid_utc).isoformat(), []).append(obs)

    window_pit = window_pit_table(pit=pit, decoded_dir=decoded_dir, asos_by_day=asos_by_day)
    window_path = out_dir / "k2_pit_window_asos.csv"
    window_pit.to_csv(window_path, index=False)
    print(f"wrote {window_path} rows={len(window_pit)}")
    print("\n=== PIT vs ASOS max inside 12Z-06Z (NBM product) ===")
    if not window_pit.empty:
        print(
            f"below_p10={window_pit['win_below_p10'].mean():.3f} "
            f"in_p10_p90={window_pit['win_in_p10_p90'].mean():.3f} "
            f"above_p90={window_pit['win_above_p90'].mean():.3f} "
            f"mae_p50={window_pit['win_high_minus_p50'].abs().mean():.3f} "
            f"bias={window_pit['win_high_minus_p50'].mean():.3f}"
        )
        for season, group in window_pit.groupby("season"):
            print(
                f"  {season} n={len(group)} in={group['win_in_p10_p90'].mean():.3f} "
                f"below={group['win_below_p10'].mean():.3f} "
                f"above={group['win_above_p90'].mean():.3f} "
                f"bias={group['win_high_minus_p50'].mean():.3f}"
            )

    settled = tradable.dropna(subset=["settled_yes", "nbm_prob", "market_mid_carryforward"])
    models = [("nbm", "nbm_prob"), ("market_cf", "market_mid_carryforward")]
    if "market_mid_normalized" in settled.columns:
        models.append(("market_cf_normalized", "market_mid_normalized"))
    murphy = pd.DataFrame(murphy_model_rows(settled, models))
    murphy_path = out_dir / "k2_murphy_brier.csv"
    murphy.to_csv(murphy_path, index=False)
    print("\n=== Murphy Brier decomposition (T-24h primary) ===")
    print(murphy.to_string(index=False))

    pit_width = pit[["climate_date", "cli_nbm_p10_f", "cli_nbm_p90_f"]].copy()
    pit_width["nbm_width_f"] = pit_width["cli_nbm_p90_f"] - pit_width["cli_nbm_p10_f"]
    joined = tradable.merge(pit_width, on="climate_date", how="left")
    joined["nbm_tercile"] = _qcut_tercile(joined["nbm_prob"])
    width_tercile = _qcut_tercile(joined["nbm_width_f"].dropna())
    joined.loc[joined["nbm_width_f"].notna(), "width_tercile"] = width_tercile

    def _taker_slice(name: str, group: pd.DataFrame) -> dict[str, Any]:
        sell = group["taker_sell_yes_edge"].dropna()
        buy = group["taker_buy_yes_edge"].dropna()
        return {
            "slice": name,
            "n": int(len(group)),
            "median_edge_cf": float(group["edge_cf"].median()) if len(group) else None,
            "median_taker_sell": float(sell.median()) if len(sell) else None,
            "frac_taker_sell_gt_0": float((sell > 0).mean()) if len(sell) else None,
            "median_taker_buy": float(buy.median()) if len(buy) else None,
            "frac_taker_buy_gt_0": float((buy > 0).mean()) if len(buy) else None,
            "median_spread": float(group["spread_carryforward"].median()) if len(group) else None,
        }

    slice_rows = [
        _taker_slice(f"nbm_{name}", group) for name, group in joined.groupby("nbm_tercile")
    ]
    if "width_tercile" in joined.columns:
        for name, group in joined.dropna(subset=["width_tercile"]).groupby("width_tercile"):
            slice_rows.append(_taker_slice(f"width_{name}", group))
    slices = pd.DataFrame(slice_rows)
    slices_path = out_dir / "k2_taker_tercile_width.csv"
    slices.to_csv(slices_path, index=False)
    print("\n=== Taker-sell by NBM tercile and ladder width ===")
    print(slices.to_string(index=False))

    labels = pd.read_csv(labels_csv) if labels_csv.exists() else pd.DataFrame()
    revisions = cli_revision_table(labels)
    rev_path = out_dir / "k2_cli_revisions.csv"
    revisions.to_csv(rev_path, index=False)
    print(f"\nwrote {rev_path} rows={len(revisions)}")
    print("=== CLI snapshot vs last full-day high ===")
    print(NOTE_LABEL)
    if not revisions.empty:
        for era, group in revisions.groupby("era"):
            if era == "first_7_or_8am":
                for hour in (7, 8):
                    col = f"snap_{hour:02d}_disagree"
                    late = f"snap_{hour:02d}_late"
                    valid = group[col].dropna()
                    print(
                        f"  {era} {hour:02d}ET n={len(group)} "
                        f"late={float(group[late].mean()):.3f} "
                        f"disagree={float(valid.mean()) if len(valid) else float('nan'):.4f} "
                        f"n_disagree={int(valid.sum()) if len(valid) else 0}"
                    )
            else:
                col = "snap_10_disagree"
                late = "snap_10_late"
                valid = group[col].dropna()
                print(
                    f"  {era} 10ET n={len(group)} "
                    f"late={float(group[late].mean()):.3f} "
                    f"disagree={float(valid.mean()) if len(valid) else float('nan'):.4f} "
                    f"n_disagree={int(valid.sum()) if len(valid) else 0}"
                )

    fee_rows = tradable.dropna(subset=["market_mid_carryforward", "half_spread", "nbm_prob"]).copy()
    fee_rows["bid"] = fee_rows["market_mid_carryforward"] - fee_rows["half_spread"]
    fee_rows["ask"] = fee_rows["market_mid_carryforward"] + fee_rows["half_spread"]
    fee_rows["fee_sell"] = fee_rows["bid"].map(quadratic_taker_fee)
    fee_rows["fee_buy"] = fee_rows["ask"].map(quadratic_taker_fee)
    fee_rows["taker_sell_after_fee"] = fee_rows["taker_sell_yes_edge"] - fee_rows["fee_sell"]
    fee_rows["taker_buy_after_fee"] = fee_rows["taker_buy_yes_edge"] - fee_rows["fee_buy"]
    fee_path = out_dir / "k2_taker_after_fee.csv"
    fee_rows.to_csv(fee_path, index=False)
    print("\n=== Quadratic taker fee at touch (C=1) ===")
    print(NOTE_FEE)
    print(
        f"median_fee_sell={fee_rows['fee_sell'].median():.4f} "
        f"median_taker_sell_after_fee={fee_rows['taker_sell_after_fee'].median():.4f} "
        f"frac_sell_after_fee>0={float((fee_rows['taker_sell_after_fee'] > 0).mean()):.3f}"
    )
    print(
        f"median_fee_buy={fee_rows['fee_buy'].median():.4f} "
        f"median_taker_buy_after_fee={fee_rows['taker_buy_after_fee'].median():.4f} "
        f"frac_buy_after_fee>0={float((fee_rows['taker_buy_after_fee'] > 0).mean()):.3f}"
    )
    for season, group in fee_rows.groupby("season"):
        print(
            f"  {season} med_sell_after_fee={group['taker_sell_after_fee'].median():.4f} "
            f"frac>0={float((group['taker_sell_after_fee'] > 0).mean()):.3f}"
        )

    logger_days = [day for day in LOGGER_DAYS if (raw_dir / day / "orderbook").is_dir()]
    print("\n=== Offline emission (logger books vs on-disk candles) ===")
    print(f"logger_days_with_books={logger_days}")
    emission_report: dict[str, Any] = {
        "logger_days": logger_days,
        "markets_compared": 0,
        "boundaries_compared": 0,
        "match_rate": None,
        "mismatch_count": 0,
        "silent_count": 0,
        "tickers_with_books_without_candles": 0,
    }
    if logger_days:
        start = date.fromisoformat(logger_days[0])
        end = date.fromisoformat(logger_days[-1])
        tickers = discover_tickers(raw_dir, logger_days)
        books = load_logger_books(raw_dir, start=start, end=end, tickers=set(tickers))
        candles_by_ticker = load_candles_for_tickers(raw_dir, set(tickers))
        compared = matched = 0
        mismatches: list[dict[str, Any]] = []
        silent: list[dict[str, Any]] = []
        missing_candles = 0
        used = 0
        for ticker in tickers:
            rows = books.get(ticker) or []
            candle_rows = candles_by_ticker.get(ticker) or []
            if not rows:
                continue
            if not candle_rows:
                missing_candles += 1
                continue
            lo = int(rows[0][0].timestamp()) - 60
            hi = int(rows[-1][0].timestamp()) + 60
            candle_rows = [
                candle
                for candle in candle_rows
                if isinstance(candle.get("end_period_ts"), (int, float))
                and lo <= int(candle["end_period_ts"]) <= hi
            ]
            if not candle_rows:
                missing_candles += 1
                continue
            used += 1
            result = compare_ticker(
                ticker=ticker,
                logger_rows=rows,
                candles=candle_rows,
                boundaries=minute_boundaries(rows[0][0], rows[-1][0]),
            )
            compared += int(result["compared"])
            matched += int(result["matched"])
            mismatches.extend(result["mismatches"])
            silent.extend(result["silent_changes"])
        match_rate = (matched / compared) if compared else None
        emission_report = {
            "logger_days": logger_days,
            "markets_discovered": len(tickers),
            "markets_compared": used,
            "tickers_with_books_without_candles": missing_candles,
            "boundaries_compared": compared,
            "match_rate": match_rate,
            "mismatch_count": len(mismatches),
            "silent_count": len(silent),
        }
        print(
            f"markets_compared={used} without_candles={missing_candles} "
            f"boundaries={compared} match_rate={match_rate} "
            f"mismatches={len(mismatches)} silent={len(silent)}"
        )
        if missing_candles and used == 0:
            print(
                "NOTE: historical candlesticks on disk do not cover the live logger "
                "tickers (Aug 2026). Offline emission cannot score those days."
            )
    pd.DataFrame([emission_report]).to_csv(out_dir / "k2_emission_offline.csv", index=False)

    print("\n=== Trades vs logger top of book ===")
    trade_rows = load_logger_trades(raw_dir, logger_days)
    trade_class: dict[str, int] = {}
    if trade_rows and logger_days:
        start = date.fromisoformat(logger_days[0])
        end = date.fromisoformat(logger_days[-1])
        tickers = {str(row["ticker"]) for row in trade_rows}
        books = load_logger_books(raw_dir, start=start, end=end, tickers=tickers)
        BookRow = tuple[datetime, float | None, float | None]
        indexed: dict[str, tuple[list[datetime], list[BookRow]]] = {}
        for ticker, series in books.items():
            indexed[ticker] = ([row[0] for row in series], series)
        classified: list[dict[str, Any]] = []
        for trade in trade_rows:
            times, series = indexed.get(str(trade["ticker"]), ([], []))
            bid = ask = None
            if times:
                idx = bisect.bisect_right(times, trade["ts_utc"]) - 1
                if idx >= 0:
                    _, bid, ask = series[idx]
            label = classify_print(float(trade["price"]), bid, ask)
            trade_class[label] = trade_class.get(label, 0) + 1
            classified.append(
                {
                    "ticker": trade["ticker"],
                    "ts_utc": trade["ts_utc"].isoformat(),
                    "price": trade["price"],
                    "count": trade["count"],
                    "bid": bid,
                    "ask": ask,
                    "class": label,
                }
            )
        pd.DataFrame(classified).to_csv(out_dir / "k2_trades_vs_quotes.csv", index=False)
        print(f"n_trades={len(classified)} classes={trade_class}")
    else:
        print("no logger trades on local days")
        pd.DataFrame(columns=["ticker", "class"]).to_csv(
            out_dir / "k2_trades_vs_quotes.csv", index=False
        )

    summary = pd.DataFrame(
        [
            {
                "metric": "win_in_p10_p90",
                "value": (
                    float(window_pit["win_in_p10_p90"].mean()) if not window_pit.empty else None
                ),
            },
            {
                "metric": "win_below_p10",
                "value": (
                    float(window_pit["win_below_p10"].mean()) if not window_pit.empty else None
                ),
            },
            {
                "metric": "win_above_p90",
                "value": (
                    float(window_pit["win_above_p90"].mean()) if not window_pit.empty else None
                ),
            },
            {
                "metric": "median_taker_sell_after_fee",
                "value": (
                    float(fee_rows["taker_sell_after_fee"].median()) if len(fee_rows) else None
                ),
            },
            {
                "metric": "frac_taker_sell_after_fee_gt_0",
                "value": (
                    float((fee_rows["taker_sell_after_fee"] > 0).mean()) if len(fee_rows) else None
                ),
            },
            {
                "metric": "emission_match_rate",
                "value": emission_report.get("match_rate"),
            },
            {
                "metric": "n_logger_trades",
                "value": float(sum(trade_class.values())) if trade_class else 0.0,
            },
        ]
    )
    summary.to_csv(out_dir / "k2_rigor_summary.csv", index=False)
    print(f"\nwrote {out_dir / 'k2_rigor_summary.csv'}")
    print(NOTE_WINDOW)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K2 rigor follow-ups on disk")
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
