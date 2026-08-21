"""Prospective depth census from VPS logger orderbook JSONL (K1 2b).

Allowed DB: none. Reads logger orderbook + markets JSONL only; never opens
heartbeat.sqlite or backfill.sqlite.

Candlesticks have no depth — this instrument uses logger captures only.
History length is logger-length (days), not years; re-run as data accrues.
Prints distributions only — no verdict language.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from analysis.orderbook import depth_metrics_from_payload
from analysis.spread_census import (
    is_two_sided,
    parse_iso_utc,
    ticker_climate_date,
)
from ingestion.climate_day import climate_day_end
from ingestion.config_loader import load_config
from ingestion.validate_units import assert_non_empty_frame
from ingestion.writer import read_jsonl_gz

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

DEFAULT_HORIZONS_H = (24, 12, 6, 3, 1)
NOTE_LOGGER_LENGTH = (
    "NOTE: depth census uses logger orderbook JSONL only (K1 2b). History is "
    "logger-length in days, not years. Re-run as the VPS logger accrues. "
    "Candlesticks carry no depth."
)


def _date_range(start: date, end: date) -> list[str]:
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def discover_logger_days(data_dir: Path) -> list[str]:
    days: list[str] = []
    if not data_dir.is_dir():
        return days
    for child in sorted(data_dir.iterdir()):
        if child.is_dir() and (child / "orderbook").is_dir():
            days.append(child.name)
    return days


def load_logger_orderbook_snapshots(
    data_dir: Path,
    *,
    start: date,
    end: date,
) -> dict[str, list[tuple[datetime, dict[str, Any]]]]:
    """Return {ticker: [(capture_ts, payload), ...]} sorted by capture time."""
    by_ticker: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for day in _date_range(start, end):
        folder = data_dir / day / "orderbook"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.jsonl.gz")):
            ticker = path.name.removesuffix(".jsonl.gz")
            rows = by_ticker.setdefault(ticker, [])
            for record in read_jsonl_gz(path):
                ts_raw = record.get("ts_utc")
                payload = record.get("payload")
                if not isinstance(ts_raw, str) or not isinstance(payload, dict):
                    continue
                captured = parse_iso_utc(ts_raw)
                if captured is None:
                    continue
                rows.append((captured, payload))
    for ticker in by_ticker:
        by_ticker[ticker].sort(key=lambda row: row[0])
    return by_ticker


def load_logger_markets(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Latest open/close metadata per ticker from logger markets captures."""
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in sorted(data_dir.glob("*/markets/*.jsonl.gz")):
        for record in read_jsonl_gz(path):
            ts_raw = record.get("ts_utc")
            payload = record.get("payload")
            if not isinstance(ts_raw, str) or not isinstance(payload, dict):
                continue
            captured = parse_iso_utc(ts_raw)
            if captured is None:
                continue
            markets = payload.get("markets")
            if isinstance(markets, list):
                for market in markets:
                    if not isinstance(market, dict):
                        continue
                    ticker = str(market.get("ticker") or "")
                    if ticker:
                        latest[ticker] = (captured, market)
            market_one = payload.get("market")
            if isinstance(market_one, dict):
                ticker = str(market_one.get("ticker") or "")
                if ticker:
                    latest[ticker] = (captured, market_one)
    return {ticker: meta for ticker, (_, meta) in latest.items()}


def snapshot_at_or_before(
    rows: list[tuple[datetime, dict[str, Any]]],
    boundary: datetime,
) -> tuple[datetime | None, dict[str, Any] | None]:
    eligible = [row for row in rows if row[0] <= boundary]
    if not eligible:
        return None, None
    return eligible[-1][0], eligible[-1][1]


def build_depth_snapshots(
    *,
    snapshots_by_ticker: dict[str, list[tuple[datetime, dict[str, Any]]]],
    markets: dict[str, dict[str, Any]],
    horizons_h: tuple[int, ...],
    primary_band: tuple[float, float],
) -> pd.DataFrame:
    low, high = primary_band
    rows: list[dict[str, Any]] = []
    for ticker, capture_rows in snapshots_by_ticker.items():
        climate_date = ticker_climate_date(ticker)
        if climate_date is None:
            continue
        market = markets.get(ticker, {})
        open_raw = market.get("open_time")
        close_raw = market.get("close_time")
        open_dt = parse_iso_utc(open_raw if isinstance(open_raw, str) else None)
        close_dt = parse_iso_utc(close_raw if isinstance(close_raw, str) else None)
        t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
        for hours in horizons_h:
            snapshot = t_end - timedelta(hours=hours)
            capture_ts, payload = snapshot_at_or_before(capture_rows, snapshot)
            if payload is None or capture_ts is None:
                continue
            metrics = depth_metrics_from_payload(payload)
            if metrics is None:
                continue
            bid, ask = metrics["bid"], metrics["ask"]
            two_sided = is_two_sided(bid, ask)
            mid = metrics["mid"]
            in_band = two_sided and low <= mid <= high
            in_window = (open_dt is None or snapshot >= open_dt) and (
                close_dt is None or snapshot <= close_dt
            )
            rows.append(
                {
                    "ticker": ticker,
                    "climate_date": climate_date,
                    "horizon_h": hours,
                    "capture_week": capture_ts.strftime("%G-W%V"),
                    "snapshot_utc": snapshot.isoformat(),
                    "capture_utc": capture_ts.isoformat(),
                    "logger_age_sec": (snapshot - capture_ts).total_seconds(),
                    "in_trading_window": in_window,
                    "two_sided": two_sided,
                    "in_primary_band": in_band,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "imbalance_top": metrics.get("imbalance_top"),
                    "yes_size_1": metrics["yes_size_1"],
                    "yes_size_5": metrics["yes_size_5"],
                    "no_size_1": metrics["no_size_1"],
                    "no_size_5": metrics["no_size_5"],
                    "depth_within_1c": metrics["depth_within_1c"],
                    "depth_within_2c": metrics["depth_within_2c"],
                    "depth_within_3c": metrics["depth_within_3c"],
                    "depth_within_5c": metrics["depth_within_5c"],
                    "dollar_depth_within_1c": metrics["dollar_depth_within_1c"],
                    "dollar_depth_within_2c": metrics["dollar_depth_within_2c"],
                    "dollar_depth_within_3c": metrics["dollar_depth_within_3c"],
                    "dollar_depth_within_5c": metrics["dollar_depth_within_5c"],
                }
            )
    return pd.DataFrame(rows)


def write_figures(snapshots: pd.DataFrame, out_dir: Path) -> list[Path]:
    paths: list[Path] = []
    primary = snapshots[snapshots["in_primary_band"] & snapshots["in_trading_window"]]
    if primary.empty:
        return paths

    fig, ax = plt.subplots(figsize=(8, 4))
    medians = primary.groupby("horizon_h")["dollar_depth_within_2c"].median()
    medians.sort_index().plot(kind="bar", ax=ax)
    ax.set_xlabel("horizon hours before climate-day end")
    ax.set_ylabel("median dollar depth within 2¢")
    ax.set_title("depth within 2¢ vs horizon (10–90¢ band, in window)")
    path = out_dir / "depth_within_2c_vs_horizon.png"
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, ax = plt.subplots(figsize=(10, 4))
    weekly = primary.groupby("capture_week")["dollar_depth_within_2c"].median()
    weekly.plot(ax=ax)
    ax.set_xlabel("capture week (ISO)")
    ax.set_ylabel("median dollar depth within 2¢")
    ax.set_title("depth by week (10–90¢ band)")
    path = out_dir / "depth_by_week.png"
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    horizon = 24 if (primary["horizon_h"] == 24).any() else int(primary["horizon_h"].min())
    subset = primary[primary["horizon_h"] == horizon]
    if not subset.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(subset["dollar_depth_within_2c"], bins=20, edgecolor="black", linewidth=0.5)
        ax.set_xlabel("dollar depth within 2¢")
        ax.set_ylabel("snapshots")
        ax.set_title(f"dollar depth distribution at T-{horizon}h")
        path = out_dir / "dollar_depth_distribution.png"
        fig.tight_layout()
        fig.savefig(path)
        plt.close(fig)
        paths.append(path)
    return paths


def run(config: dict[str, Any], data_dir: Path, out_dir: Path) -> int:
    depth_cfg = config.get("depth_census") or {}
    horizons = tuple(depth_cfg.get("horizons_h") or DEFAULT_HORIZONS_H)
    band = depth_cfg.get("primary_band") or [0.10, 0.90]
    primary_band = (float(band[0]), float(band[1]))
    out_dir.mkdir(parents=True, exist_ok=True)

    days = discover_logger_days(data_dir)
    if not days:
        print("no logger orderbook days found under data_dir")
        print(NOTE_LOGGER_LENGTH)
        return 0

    start = date.fromisoformat(days[0])
    end = date.fromisoformat(days[-1])
    snapshots_by_ticker = load_logger_orderbook_snapshots(data_dir, start=start, end=end)
    markets = load_logger_markets(data_dir)
    table = build_depth_snapshots(
        snapshots_by_ticker=snapshots_by_ticker,
        markets=markets,
        horizons_h=horizons,
        primary_band=primary_band,
    )

    print(NOTE_LOGGER_LENGTH)
    print(f"logger_days={len(days)} span={days[0]}..{days[-1]} tickers={len(snapshots_by_ticker)}")
    if table.empty:
        print("no depth snapshots at configured horizons")
        return 0

    csv_path = out_dir / "depth_census.csv"
    assert_non_empty_frame(table, what="depth_census.csv")
    table.to_csv(csv_path, index=False)
    print(f"wrote {csv_path} rows={len(table)}")

    primary = table[table["in_primary_band"] & table["in_trading_window"]]
    if not primary.empty:
        print("median dollar_depth_within_2c by horizon (10–90¢, in window):")
        summary = primary.groupby("horizon_h")["dollar_depth_within_2c"].median()
        print(summary.to_string())

    pngs = write_figures(table, out_dir)
    for path in pngs:
        print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Logger depth census (K1 2b)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    storage = config["storage"]
    data_dir = args.data_dir or Path(storage["raw_dir"])
    depth_cfg = config.get("depth_census") or {}
    out_dir = args.out_dir or Path(str(depth_cfg.get("out_dir") or "analysis/out"))
    return run(config, data_dir, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
