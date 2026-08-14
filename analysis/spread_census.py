"""Spread census over historical Kalshi KXHIGHNY books.

Allowed DB: none. Reads raw JSONL + clinyc.csv only; never opens
heartbeat.sqlite or backfill.sqlite.

Label selection (session-2 simplification, not §1.3): latest issuance that
covers the full climate day (drop VALID-TODAY intermediates). Kalshi
settlement-snapshot timing is session 3.

Clock B (pre_max / post_max) treats printed CLINYC max times as carrying
±1 hour uncertainty until summer LST-vs-LDT is verified against KNYC ASOS.

Two-sided means bid >= $0.01 and ask <= $0.99 (A6). Kalshi renders an empty
book as bid 0.00 / ask 1.00; those snapshots are counted as no-market, not as
a 99-cent spread, and never reach the spread statistics.

Horizons run T-48h to T-1h (A7). T-48h is kept even though markets open ~38h
before T, because its empty column documents that fact; T-36h is the earliest
horizon with real books. Rows carry an `era` column so the structurally
different 2021 market never pools with 2022+.

This script prints distributions only — no thresholds, no pass/fail.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from ingestion.climate_day import LST, MONTHS, climate_day_end, parse_lst_clock
from ingestion.config_loader import load_config
from ingestion.writer import read_jsonl_gz

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

# A7: markets open ~38h before the climate-day end, so T-48h predates open almost
# everywhere and its near-zero coverage is a structural fact worth showing. T-36h
# is the earliest horizon that actually exists (~1 PM ET the day before).
HORIZONS_H = (48, 36, 24, 12, 6, 3, 1)
# The 2021 market was structurally different (~1.3 brackets/day vs ~6/day from
# 2022 on), so it is grouped separately instead of pooling with the modern regime.
EARLY_ERA_LAST_YEAR = 2021
STALE_SEC = 15 * 60
BANDS = (("10_90", 0.10, 0.90), ("20_80", 0.20, 0.80))
# A6: bid 0.00 / ask 1.00 is an empty book rendered as extreme quotes.
MIN_BID_DOLLARS = 0.01
MAX_ASK_DOLLARS = 0.99
TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")
NOTE_T48_STRUCTURAL = (
    "NOTE: markets open ~38h before the climate-day end, so near-zero coverage at "
    "T-48h is structural (the market was not open yet), not a data gap. T-36h is "
    "the earliest horizon that exists in practice."
)
NOTE_ERA_SPLIT = (
    "NOTE: rows are split by era. 2021 traded ~1.3 brackets/day vs ~6/day from 2022 "
    "on; do not pool 2021 with the modern regime at readout."
)
OPEN_ITEM_LST = (
    "OPEN: verify printed CLINYC MAXIMUM times against IEM ASOS hourly KNYC "
    "for ~3 summer days — is the TIME column LST or LDT in summer issuances? "
    "Until then Clock B (pre_max/post_max) carries ±1h uncertainty."
)


def ticker_climate_date(ticker: str) -> str | None:
    match = TICKER_DATE.search(ticker)
    if not match:
        return None
    year = 2000 + int(match.group(1))
    month = MONTHS.get(match.group(2))
    if month is None:
        return None
    return f"{year:04d}-{month:02d}-{int(match.group(3)):02d}"


def iter_category(raw_dir: Path, category: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in raw_dir.glob(f"*/{category}/*.jsonl.gz"):
        records.extend(read_jsonl_gz(path))
    return records


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def candle_fields(candle: dict[str, Any]) -> dict[str, Any]:
    bid = candle.get("yes_bid") if isinstance(candle.get("yes_bid"), dict) else {}
    ask = candle.get("yes_ask") if isinstance(candle.get("yes_ask"), dict) else {}
    return {
        "end_period_ts": candle.get("end_period_ts"),
        "bid_close": _float(bid.get("close_dollars", bid.get("close"))),
        "ask_close": _float(ask.get("close_dollars", ask.get("close"))),
        "volume": _float(candle.get("volume_fp", candle.get("volume"))),
    }


def is_two_sided(bid: float | None, ask: float | None) -> bool:
    """A6: a real two-sided book needs bid >= $0.01 and ask <= $0.99.

    Kalshi renders an *empty* book as bid 0.00 / ask 1.00. Presence-checking
    alone would score that as a 99-cent spread instead of "no market", and a
    0.00 bid is absence, not a price. Both sides must be inside the bounds
    before the snapshot feeds spread statistics.
    """
    if bid is None or ask is None:
        return False
    return bid >= MIN_BID_DOLLARS and ask <= MAX_ASK_DOLLARS


def last_candle_at_or_before(
    candles: list[dict[str, Any]], snapshot_ts: int
) -> dict[str, Any] | None:
    eligible = [
        c
        for c in candles
        if isinstance(c.get("end_period_ts"), (int, float))
        and int(c["end_period_ts"]) <= snapshot_ts
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda c: int(c["end_period_ts"]))


def is_stale(candle: dict[str, Any], snapshot_ts: int, stale_sec: int = STALE_SEC) -> bool:
    end_ts = candle.get("end_period_ts")
    if not isinstance(end_ts, (int, float)):
        return True
    return (snapshot_ts - int(end_ts)) > stale_sec


def select_full_day_labels(csv_path: Path) -> pd.DataFrame:
    labels = pd.read_csv(csv_path)
    if labels.empty:
        return labels
    flag = labels["is_same_day_intermediate"]
    if flag.dtype == object:
        flag = flag.astype(str).str.lower().isin(["1", "true", "yes"])
    full = labels.loc[~flag.astype(bool)].copy()
    full = full.sort_values("issuance_ts_utc")
    return full.groupby("climate_date", as_index=False).tail(1)


def era_of(climate_date: str) -> str:
    year = int(climate_date[:4])
    if year <= EARLY_ERA_LAST_YEAR:
        return f"{EARLY_ERA_LAST_YEAR}_early"
    return f"{EARLY_ERA_LAST_YEAR + 1}_plus"


def season_of(climate_date: str) -> str:
    month = int(climate_date[5:7])
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def regime_at_snapshot(
    *,
    snapshot: datetime,
    climate_date: str,
    time_of_high_raw: str,
) -> tuple[str | None, bool]:
    """Return (pre_max|post_max|None, uncertain).

    Printed max time is used as a clock on the climate date in LST. Until the
    LST-vs-LDT check, any snapshot within ±1h of that clock is marked uncertain.
    """
    parsed = parse_lst_clock(str(time_of_high_raw or ""))
    if parsed is None:
        return None, True
    hour, minute = parsed
    year, month, day = (int(p) for p in climate_date.split("-"))
    max_lst = datetime(year, month, day, hour, minute, tzinfo=LST)
    snap_lst = snapshot.astimezone(LST)
    delta = abs((snap_lst - max_lst).total_seconds())
    uncertain = delta <= 3600
    regime = "pre_max" if snap_lst < max_lst else "post_max"
    return regime, uncertain


def load_markets(raw_dir: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in iter_category(raw_dir, "markets_history"):
        payload = record.get("payload") or {}
        markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict) or not market.get("ticker"):
                continue
            seen[str(market["ticker"])] = market
    return list(seen.values())


def _ticker_from_candle_record(record: dict[str, Any], payload: dict[str, Any]) -> str:
    raw = payload.get("ticker")
    if raw:
        return str(raw)
    endpoint = str(record.get("endpoint") or "").strip("/")
    parts = endpoint.split("/")
    if "markets" in parts:
        idx = parts.index("markets")
        if idx + 1 < len(parts) and parts[idx + 1] not in {"candlesticks", "trades", "orderbook"}:
            return parts[idx + 1]
    return ""


def load_candles(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for record in iter_category(raw_dir, "candlesticks"):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        ticker = _ticker_from_candle_record(record, payload)
        if not ticker:
            continue
        candles = payload.get("candlesticks")
        if not isinstance(candles, list):
            continue
        bucket = by_ticker.setdefault(ticker, [])
        for candle in candles:
            if isinstance(candle, dict):
                bucket.append(candle_fields(candle))
    return by_ticker


def build_snapshot_table(
    *,
    markets: list[dict[str, Any]],
    candles_by_ticker: dict[str, list[dict[str, Any]]],
    labels: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float]]:
    label_map = {}
    if not labels.empty:
        for row in labels.itertuples(index=False):
            label_map[str(row.climate_date)] = row
    rows: list[dict[str, Any]] = []
    stale_n = 0
    quote_attempts = 0
    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate_date = ticker_climate_date(ticker)
        if not climate_date:
            continue
        t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
        open_ts = market.get("open_time")
        close_ts = market.get("close_time")
        candles = candles_by_ticker.get(ticker, [])
        label = label_map.get(climate_date)
        for hours in HORIZONS_H:
            snapshot = t_end - timedelta(hours=hours)
            snapshot_ts = int(snapshot.timestamp())
            quote_attempts += 1
            candle = last_candle_at_or_before(candles, snapshot_ts)
            stale = False
            if candle is None:
                valid = False
            elif is_stale(candle, snapshot_ts):
                valid = False
                stale = True
                stale_n += 1
            else:
                valid = True
            bid = candle["bid_close"] if candle else None
            ask = candle["ask_close"] if candle else None
            two_sided = valid and is_two_sided(bid, ask)
            mid = (bid + ask) / 2.0 if two_sided else None
            spread = (ask - bid) if two_sided else None
            book_present = bid is not None and ask is not None
            extreme_only = valid and book_present and not two_sided
            regime, uncertain = (None, True)
            if label is not None:
                regime, uncertain = regime_at_snapshot(
                    snapshot=snapshot,
                    climate_date=climate_date,
                    time_of_high_raw=str(getattr(label, "time_of_high_raw", "") or ""),
                )
            rows.append(
                {
                    "ticker": ticker,
                    "climate_date": climate_date,
                    "horizon_h": hours,
                    "season": season_of(climate_date),
                    "era": era_of(climate_date),
                    "regime": regime,
                    "regime_uncertain": uncertain,
                    "quote_valid": valid,
                    "stale": stale,
                    "two_sided": two_sided,
                    "book_fields_present": book_present,
                    "extreme_empty_book": extreme_only,
                    "mid": mid,
                    "spread": spread,
                    "volume": candle["volume"] if candle else None,
                    "open_time": open_ts,
                    "close_time": close_ts,
                    "high_F": getattr(label, "high_F", None) if label is not None else None,
                }
            )
    stats = {
        "quote_attempts": float(quote_attempts),
        "stale_discards": float(stale_n),
        "stale_discard_rate": (stale_n / quote_attempts) if quote_attempts else 0.0,
    }
    return pd.DataFrame(rows), stats


def summarize(snapshots: pd.DataFrame) -> pd.DataFrame:
    if snapshots.empty:
        return snapshots
    rows: list[dict[str, Any]] = []
    for band_name, lo, hi in BANDS:
        tagged = snapshots.copy()
        tagged["in_band"] = tagged["two_sided"] & tagged["mid"].between(lo, hi)
        grouped = tagged.groupby(
            ["horizon_h", "era", "season", "regime", "regime_uncertain"],
            dropna=False,
        )
        for keys, group in grouped:
            horizon_h, era, season, regime, uncertain = keys
            n_market_days = group[["ticker", "climate_date"]].drop_duplicates().shape[0]
            n_days = group["climate_date"].nunique()
            coverage = float(group["quote_valid"].mean()) if len(group) else 0.0
            two_sided_share = float(group["two_sided"].mean()) if len(group) else 0.0
            quoted = group[group["quote_valid"]]
            two_sided_given_quote = float(quoted["two_sided"].mean()) if len(quoted) else 0.0
            empty_book_share = float(group["extreme_empty_book"].mean()) if len(group) else 0.0
            spread_rows = group[group["in_band"] & group["spread"].notna()]
            per_day = (
                group.groupby("climate_date")["in_band"].sum()
                if len(group)
                else pd.Series(dtype=float)
            )
            rows.append(
                {
                    "horizon_h": horizon_h,
                    "era": era,
                    "season": season,
                    "regime": regime,
                    "regime_uncertain": uncertain,
                    "band": band_name,
                    "n_snapshots": int(len(group)),
                    "n_market_days": int(n_market_days),
                    "n_days": int(n_days),
                    "coverage": coverage,
                    "two_sided_share": two_sided_share,
                    "two_sided_share_given_quote": two_sided_given_quote,
                    "extreme_empty_book_share": empty_book_share,
                    "tradeable_brackets_per_day_median": (
                        float(per_day.median()) if len(per_day) else 0.0
                    ),
                    "median_spread": (
                        float(spread_rows["spread"].median()) if len(spread_rows) else None
                    ),
                    "p25": (
                        float(spread_rows["spread"].quantile(0.25)) if len(spread_rows) else None
                    ),
                    "p75": (
                        float(spread_rows["spread"].quantile(0.75)) if len(spread_rows) else None
                    ),
                    "p90": (
                        float(spread_rows["spread"].quantile(0.90)) if len(spread_rows) else None
                    ),
                    "n_spread_obs": int(len(spread_rows)),
                    "volume_sum": float(group["volume"].fillna(0).sum()),
                }
            )
    return pd.DataFrame(rows)


def write_figures(snapshots: pd.DataFrame, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    two_sided = snapshots[snapshots["two_sided"] & snapshots["spread"].notna()]

    fig, ax = plt.subplots()
    if not two_sided.empty:
        stats = two_sided.groupby("horizon_h")["spread"].median()
        ax.plot(stats.index, stats.values, marker="o")
    ax.set_xlabel("horizon hours before climate-day end")
    ax.set_ylabel("median spread (dollars)")
    ax.set_title("spread vs horizon")
    path = out_dir / "spread_vs_horizon.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, ax = plt.subplots()
    if not two_sided.empty:
        two_sided.boxplot(column="spread", by="season", ax=ax)
        fig.suptitle("")
    ax.set_title("spread by season")
    ax.set_ylabel("spread (dollars)")
    path = out_dir / "spread_by_season.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, ax = plt.subplots()
    if not snapshots.empty:
        daily = snapshots[snapshots["horizon_h"] == 1].groupby("climate_date")["two_sided"].sum()
        if not daily.empty:
            ax.plot(pd.to_datetime(daily.index), daily.values, marker=".", linestyle="none")
    ax.set_title("two-sided brackets per day (T-1h)")
    ax.set_ylabel("count")
    path = out_dir / "brackets_per_day.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)

    fig, ax = plt.subplots()
    if not snapshots.empty:
        share = snapshots.groupby("horizon_h")["two_sided"].mean()
        ax.plot(share.index, share.values, marker="o")
    ax.set_xlabel("horizon hours before climate-day end")
    ax.set_ylabel("two-sided share")
    ax.set_title("two-sidedness vs horizon")
    path = out_dir / "twosided_vs_horizon.png"
    fig.savefig(path)
    plt.close(fig)
    paths.append(path)
    return paths


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    markets = load_markets(raw_dir)
    candles = load_candles(raw_dir)
    snapshots, stale_stats = build_snapshot_table(
        markets=markets,
        candles_by_ticker=candles,
        labels=labels,
    )
    summary = summarize(snapshots)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "spread_census.csv"
    summary.to_csv(csv_path, index=False)
    pngs = write_figures(snapshots, out_dir)
    print(
        json.dumps({k: round(v, 6) if isinstance(v, float) else v for k, v in stale_stats.items()})
    )
    print(NOTE_T48_STRUCTURAL)
    print(NOTE_ERA_SPLIT)
    print(OPEN_ITEM_LST)
    print(f"wrote {csv_path}")
    for path in pngs:
        print(f"wrote {path}")
    if not summary.empty:
        print(summary.to_string(index=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spread census (distributions only)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(config, args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
