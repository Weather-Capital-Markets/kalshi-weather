"""Locate the dates on which KXHIGHNY/HIGHNY contract conventions changed.

Allowed DB: none. Reads raw JSONL only; never opens heartbeat.sqlite or
backfill.sqlite.

Two conventions moved during the captured history and both change what a
horizon means:

  * Last trading time. Measured as an offset from the LST climate-day end,
    `close_time` alternates between 61 and 1 minutes twice a year. That is
    daylight saving, not a venue decision: a close pinned to 11:59 PM *civil*
    ET lands 61 minutes before the LST day end under EDT and 1 minute before it
    under EST. Reporting the offset alone would manufacture a changeover every
    March and November, so the script also classifies each close against both
    candidate conventions and looks for the transition only on the EDT days
    where the two disagree. Under EST they coincide and identify nothing.
  * Settlement snapshot. The contract text names the snapshot after which
    expiration occurs. An earlier snapshot cannot see a CLI revision a later
    one can.

Change points are read off the data rather than assumed: the script walks the
date-ordered sequence of daily modal values and reports every transition, so a
second undetected change cannot hide behind the first.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.spread_census import load_markets, parse_iso_utc, ticker_climate_date
from ingestion.climate_day import LST, NY_CIVIL, climate_day_end
from ingestion.config_loader import load_config

logger = logging.getLogger(__name__)

# The legacy wording is "the first 10:00 AM following"; the modern one inserts
# "ET". Requiring "ET" silently reclassified every legacy market as unspecified.
SETTLEMENT_PHRASE = re.compile(
    r"first\s+(.+?)\s+(?:ET\s+)?following",
    re.IGNORECASE,
)
LEGACY_RULEBOOK = re.compile(r"Rule\s+100\.19", re.IGNORECASE)
UNSPECIFIED = "unspecified (rulebook reference only)"
LAST_TRADE_CLOCK = "23:59"
CIVIL_ET = "civil_et_2359"
LST_FIXED = "lst_2359_fixed_utc"
BOTH_AGREE = "both (EST, not identifying)"
OTHER = "other"


def close_offset_minutes(market: dict[str, Any]) -> float | None:
    """Minutes from close_time to the LST climate-day end (positive = before)."""
    climate_date = ticker_climate_date(str(market.get("ticker") or ""))
    close_dt = parse_iso_utc(market.get("close_time"))
    if climate_date is None or close_dt is None:
        return None
    t_end = climate_day_end(datetime.fromisoformat(climate_date).date())
    return (t_end - close_dt).total_seconds() / 60.0


def is_dst_transition_day(climate_date: str) -> bool:
    """True when the NY civil offset changes inside this climate day.

    The venue prices the close off an offset that is already stale on the
    transition day itself, so every March and November boundary day sits an
    hour away from both candidate rules. Those days say nothing about which
    rule is in force and would otherwise register as changeovers.
    """
    day = datetime.fromisoformat(climate_date).date()
    start = climate_day_end(day) - timedelta(days=1)
    end = climate_day_end(day)
    return start.astimezone(NY_CIVIL).utcoffset() != end.astimezone(NY_CIVIL).utcoffset()


def close_convention(market: dict[str, Any]) -> str:
    """Which last-trading-time rule this close_time is consistent with.

    Under EST the civil-ET and fixed-UTC rules produce the same instant, so
    those days are labelled non-identifying rather than assigned to either.
    """
    close_dt = parse_iso_utc(market.get("close_time"))
    if close_dt is None:
        return OTHER
    civil = close_dt.astimezone(NY_CIVIL).strftime("%H:%M") == LAST_TRADE_CLOCK
    lst = close_dt.astimezone(LST).strftime("%H:%M") == LAST_TRADE_CLOCK
    if civil and lst:
        return BOTH_AGREE
    if civil:
        return CIVIL_ET
    if lst:
        return LST_FIXED
    return f"{OTHER} ({close_dt.astimezone(LST).strftime('%H:%M')} LST)"


def settlement_phrase(market: dict[str, Any]) -> str:
    text = " ".join(
        str(market.get(key) or "")
        for key in ("early_close_condition", "rules_secondary", "rules_primary")
    )
    normalized = " ".join(text.split())
    match = SETTLEMENT_PHRASE.search(normalized)
    if match:
        return " ".join(match.group(1).split()).upper()
    if LEGACY_RULEBOOK.search(normalized):
        return UNSPECIFIED
    return UNSPECIFIED


def daily_modal_values(rows: pd.DataFrame, value_column: str) -> pd.Series:
    """One value per climate date, so a single odd market cannot move a boundary."""
    return (
        rows.groupby("climate_date")[value_column]
        .agg(lambda values: Counter(values).most_common(1)[0][0])
        .sort_index()
    )


def change_points(daily: pd.Series) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    dates = list(daily.index)
    values = list(daily.values)
    for idx in range(1, len(values)):
        if values[idx] == values[idx - 1]:
            continue
        before = daily.iloc[:idx]
        after = daily.iloc[idx:]
        points.append(
            {
                "last_date_before": dates[idx - 1],
                "first_date_after": dates[idx],
                "value_before": values[idx - 1],
                "value_after": values[idx],
                "days_before": int((before == values[idx - 1]).sum()),
                "days_after": int((after == values[idx]).sum()),
            }
        )
    return points


def _value_spans(daily: pd.Series) -> pd.DataFrame:
    frame = daily.reset_index()
    frame.columns = ["climate_date", "value"]
    return (
        frame.groupby("value")
        .agg(
            n_days=("climate_date", "count"),
            first_date=("climate_date", "min"),
            last_date=("climate_date", "max"),
        )
        .reset_index()
        .sort_values("first_date")
    )


def build_table(markets: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate_date = ticker_climate_date(ticker)
        offset = close_offset_minutes(market)
        if climate_date is None or offset is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "climate_date": climate_date,
                "close_offset_min": round(offset),
                "close_convention": close_convention(market),
                "dst_transition_day": is_dst_transition_day(climate_date),
                "settlement_phrase": settlement_phrase(market),
            }
        )
    return pd.DataFrame(rows)


def _markdown(
    *,
    offset_spans: pd.DataFrame,
    convention_spans: pd.DataFrame,
    convention_points: list[dict[str, Any]],
    settlement_points: list[dict[str, Any]],
    settlement_spans: pd.DataFrame,
) -> str:
    lines = ["", "=== paste-ready markdown for knowledge/venue-facts.md ===", ""]
    lines.append(
        "`close_time` measured against the LST climate-day end alternates with "
        "daylight saving, so the offset alone is not an era marker:"
    )
    lines.append("")
    lines.append("| close_time offset | climate days | first | last |")
    lines.append("| --- | --- | --- | --- |")
    for row in offset_spans.itertuples(index=False):
        lines.append(
            f"| {int(row.value)} min before day end | {row.n_days} | "
            f"{row.first_date} | {row.last_date} |"
        )
    lines.append("")
    lines.append(
        "Classifying each close against both candidate rules separates the "
        "seasonal swing from a real change. EST days are non-identifying "
        "because the two rules coincide there:"
    )
    lines.append("")
    lines.append("| last-trading-time rule | climate days | first | last |")
    lines.append("| --- | --- | --- | --- |")
    for row in convention_spans.itertuples(index=False):
        lines.append(f"| {row.value} | {row.n_days} | {row.first_date} | {row.last_date} |")
    lines.append("")
    if convention_points:
        for point in convention_points:
            lines.append(
                f"Changeover: {point['value_before']} -> {point['value_after']} "
                f"between climate days {point['last_date_before']} and "
                f"{point['first_date_after']} (identifying EDT days only)."
            )
    else:
        lines.append("No changeover: one rule holds across every identifying day.")
    lines.append("")
    lines.append("Settlement snapshot wording, by era:")
    lines.append("")
    lines.append("| contract phrase | climate days | first | last |")
    lines.append("| --- | --- | --- | --- |")
    for row in settlement_spans.itertuples(index=False):
        lines.append(f"| {row.value} | {row.n_days} | {row.first_date} | {row.last_date} |")
    lines.append("")
    for point in settlement_points:
        lines.append(
            f'Changeover: "{point["value_before"]}" -> "{point["value_after"]}" '
            f"between climate days {point['last_date_before']} and "
            f"{point['first_date_after']}."
        )
    return "\n".join(lines)


def _report_change_points(points: list[dict[str, Any]], *, unit: str) -> None:
    for point in points:
        print(
            f"changeover: {point['value_before']}{unit} -> {point['value_after']}{unit} "
            f"between {point['last_date_before']} and {point['first_date_after']} "
            f"({point['days_before']} days before, {point['days_after']} days after)"
        )
    if not points:
        print("no changeover detected; the value is constant across the capture")


def run(config: dict[str, Any]) -> int:
    raw_dir = Path(config["storage"]["raw_dir"])
    table = build_table(load_markets(raw_dir))
    if table.empty:
        print("no markets with a parseable ticker date and close_time")
        return 1
    print(f"markets={len(table)} climate_days={table['climate_date'].nunique()}")

    print()
    print("=== close_time offset from the LST climate-day end (minutes before) ===")
    print("This swings with daylight saving; see the convention table below.")
    offset_daily = daily_modal_values(table, "close_offset_min")
    offset_spans = _value_spans(offset_daily)
    print(offset_spans.to_string(index=False))
    print()
    _report_change_points(change_points(offset_daily), unit=" min")

    print()
    print("=== last-trading-time rule (EST days do not identify) ===")
    settled = table[~table["dst_transition_day"]]
    convention_spans = _value_spans(daily_modal_values(settled, "close_convention"))
    print(convention_spans.to_string(index=False))
    anomalies = table.loc[table["dst_transition_day"], "climate_date"].drop_duplicates()
    print()
    print(
        f"excluded {len(anomalies)} daylight-saving transition climate days, where the "
        "close sits an hour from both rules: "
        f"{', '.join(sorted(anomalies))}"
    )
    identifying = settled[settled["close_convention"].isin({CIVIL_ET, LST_FIXED})]
    if identifying.empty:
        convention_points: list[dict[str, Any]] = []
        print("no identifying (EDT) days; the rule cannot be pinned down")
    else:
        convention_daily_identifying = daily_modal_values(identifying, "close_convention")
        convention_points = change_points(convention_daily_identifying)
        print(
            f"identifying days={len(convention_daily_identifying)} "
            f"({convention_daily_identifying.index.min()} to "
            f"{convention_daily_identifying.index.max()})"
        )
        _report_change_points(convention_points, unit="")

    print()
    print("=== settlement snapshot wording ===")
    settlement_daily = daily_modal_values(table, "settlement_phrase")
    settlement_spans = _value_spans(settlement_daily)
    print(settlement_spans.to_string(index=False))
    print()
    _report_change_points(change_points(settlement_daily), unit="")
    settlement_points = change_points(settlement_daily)

    print(
        _markdown(
            offset_spans=offset_spans,
            convention_spans=convention_spans,
            convention_points=convention_points,
            settlement_points=settlement_points,
            settlement_spans=settlement_spans,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Venue convention changeover dates")
    parser.add_argument("--config", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run(load_config(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
