"""Measure how often CLI time-of-high falls outside the NBM max window.

Allowed DB: none. Reads clinyc.csv only.

When climate.cli_time_convention is unknown, prints both LST and LDT hypothesis
rates by season and refuses a single headline number.

K2 extension (window_mismatch_k2.csv): ASOS KNYC daily-max time vs the same
12Z–06Z window, conditional whole-°F delta_f, and Polymarket bracket disagreement
between CLI high_F and ASOS max. Both LST and LDT CLI clock hypotheses — no pick.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from analysis.spread_census import season_of, select_full_day_labels
from analysis.station_basis import polymarket_bracket, whole_f
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
from ingestion.validate_units import assert_non_empty_frame

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)


def _mismatch_rows(labels: pd.DataFrame, convention: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in labels.itertuples(index=False):
        climate_date = str(row.climate_date)
        time_raw = str(getattr(row, "time_of_high_raw", "") or "")
        instant = cli_max_instant(climate_date, time_raw, convention)  # type: ignore[arg-type]
        if instant is None:
            continue
        start, end = nbm_max_window_utc(climate_date)
        outside = not time_in_window(instant, start, end)
        rows.append(
            {
                "climate_date": climate_date,
                "season": season_of(climate_date),
                "convention": convention,
                "outside_window": outside,
            }
        )
    return pd.DataFrame(rows)


def summarize_by_season(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    grouped = frame.groupby(["season", "convention"], as_index=False)["outside_window"].mean()
    grouped = grouped.rename(columns={"outside_window": "mismatch_rate"})
    return grouped


def _parse_cli_high_f(row: Any) -> float | None:
    """Return a finite CLI high, or None for missing/MM/NaN CSV cells."""
    raw = getattr(row, "high_F", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        if bool(pd.isna(raw)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def build_k2_day_rows(labels: pd.DataFrame, raw_dir: Path) -> pd.DataFrame:
    """Per-day ASOS + CLI rows for K2 window-mismatch diagnostics."""
    observations = load_asos_observations_from_raw(raw_dir, station="NYC")
    obs_by_day: dict[str, list] = {}
    for obs in observations:
        climate = climate_date_of(obs.valid_utc).isoformat()
        obs_by_day.setdefault(climate, []).append(obs)

    rows: list[dict[str, Any]] = []
    for row in labels.itertuples(index=False):
        climate_date = str(row.climate_date)
        time_raw = str(getattr(row, "time_of_high_raw", "") or "")
        if not time_raw.strip():
            continue
        day_obs = obs_by_day.get(climate_date, [])
        asos_max_f, asos_max_ts = asos_max_for_climate_day(day_obs, climate_date)
        if asos_max_ts is None or asos_max_f is None or not math.isfinite(asos_max_f):
            continue
        window_start, window_end = nbm_max_window_utc(climate_date)
        asos_outside = not time_in_window(asos_max_ts, window_start, window_end)

        cli_high = _parse_cli_high_f(row)
        cli_whole = whole_f(cli_high) if cli_high is not None else None
        asos_whole = whole_f(asos_max_f)
        delta_f = (asos_whole - cli_whole) if cli_whole is not None else None
        bracket_disagree = (
            polymarket_bracket(cli_whole) != polymarket_bracket(asos_whole)
            if cli_whole is not None
            else None
        )

        record: dict[str, Any] = {
            "climate_date": climate_date,
            "season": season_of(climate_date),
            "asos_max_f": asos_max_f,
            "asos_max_utc": asos_max_ts.isoformat(),
            "asos_outside_window": asos_outside,
            "cli_high_f": cli_high,
            "cli_whole_f": cli_whole,
            "asos_whole_f": asos_whole,
            "delta_f": delta_f,
            "bracket_disagree": bracket_disagree,
        }
        for hypothesis in ("lst", "ldt"):
            instant = cli_max_instant(climate_date, time_raw, hypothesis)
            outside = instant is not None and not time_in_window(instant, window_start, window_end)
            record[f"cli_outside_{hypothesis}"] = outside
        rows.append(record)
    return pd.DataFrame(rows)


def _median_or_nan(series: pd.Series) -> float:
    clean = series.dropna()
    if clean.empty:
        return float("nan")
    return float(clean.median())


def summarize_k2_by_season(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    seasons = sorted(frame["season"].unique())
    rows: list[dict[str, Any]] = []
    for season in seasons:
        subset = frame[frame["season"] == season]
        rows.append(_k2_summary_row(subset, season))
    rows.append(_k2_summary_row(frame, "ALL"))
    return pd.DataFrame(rows)


def _k2_summary_row(subset: pd.DataFrame, season: str) -> dict[str, Any]:
    n = len(subset)
    bracket = subset["bracket_disagree"].dropna()
    outside_lst = subset[subset["cli_outside_lst"]]
    outside_ldt = subset[subset["cli_outside_ldt"]]
    outside_asos = subset[subset["asos_outside_window"]]
    cli_lst_rate = round(float(subset["cli_outside_lst"].mean()), 4) if n else float("nan")
    cli_ldt_rate = round(float(subset["cli_outside_ldt"].mean()), 4) if n else float("nan")
    asos_rate = round(float(subset["asos_outside_window"].mean()), 4) if n else float("nan")
    return {
        "season": season,
        "n_days": n,
        "cli_outside_rate_lst": cli_lst_rate,
        "cli_outside_rate_ldt": cli_ldt_rate,
        "asos_outside_rate": asos_rate,
        "bracket_disagree_rate": round(float(bracket.mean()), 4) if len(bracket) else float("nan"),
        "delta_f_median_when_cli_lst_outside": round(_median_or_nan(outside_lst["delta_f"]), 3),
        "delta_f_median_when_cli_ldt_outside": round(_median_or_nan(outside_ldt["delta_f"]), 3),
        "delta_f_median_when_asos_outside": round(_median_or_nan(outside_asos["delta_f"]), 3),
        "bracket_disagree_rate_when_cli_lst_outside": round(
            float(outside_lst["bracket_disagree"].dropna().mean()), 4
        )
        if len(outside_lst["bracket_disagree"].dropna())
        else float("nan"),
        "bracket_disagree_rate_when_asos_outside": round(
            float(outside_asos["bracket_disagree"].dropna().mean()), 4
        )
        if len(outside_asos["bracket_disagree"].dropna())
        else float("nan"),
    }


def _write_k2_figure(summary: pd.DataFrame, path: Path) -> None:
    if summary.empty:
        return
    plot_rows = summary[summary["season"] != "ALL"].copy()
    if plot_rows.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    seasons = plot_rows["season"].tolist()
    x = range(len(seasons))
    width = 0.25
    ax.bar(
        [i - width for i in x],
        plot_rows["cli_outside_rate_lst"],
        width=width,
        label="CLI lst outside",
    )
    ax.bar(
        list(x),
        plot_rows["cli_outside_rate_ldt"],
        width=width,
        label="CLI ldt outside",
    )
    ax.bar(
        [i + width for i in x],
        plot_rows["asos_outside_rate"],
        width=width,
        label="ASOS outside",
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels(seasons)
    ax.set_ylabel("outside-window rate")
    ax.set_xlabel("season")
    ax.set_title("Time-of-max outside NBM 12Z–06Z window (K2)")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def run_k2(config: dict[str, Any], labels: pd.DataFrame, out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    if labels.empty:
        print("K2 window mismatch: no full-day labels")
        return 0
    day_rows = build_k2_day_rows(labels, raw_dir)
    if day_rows.empty:
        print("K2 window mismatch: no labeled days with ASOS coverage")
        return 0
    summary = summarize_k2_by_season(day_rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "window_mismatch_k2.csv"
    assert_non_empty_frame(summary, what="window_mismatch_k2.csv")
    summary.to_csv(csv_path, index=False)
    png_path = out_dir / "window_mismatch_k2.png"
    _write_k2_figure(summary, png_path)
    print("\n=== K2 window mismatch (CLI lst/ldt + ASOS KNYC) ===")
    print(summary.to_string(index=False))
    print(f"wrote {csv_path}")
    print(f"wrote {png_path}")
    return 0


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    labels = select_full_day_labels(labels_csv) if labels_csv.exists() else pd.DataFrame()
    convention = load_cli_time_convention(config)
    out_dir.mkdir(parents=True, exist_ok=True)

    if labels.empty:
        print("no full-day labels available")
        return 0

    if convention == "unknown":
        print("REFUSED: convention=unknown — printing both hypotheses, no headline rate")
        lst = _mismatch_rows(labels, "lst")
        ldt = _mismatch_rows(labels, "ldt")
        summary = summarize_by_season(pd.concat([lst, ldt], ignore_index=True))
        print(summary.to_string(index=False))
        csv_path = out_dir / "window_mismatch.csv"
        assert_non_empty_frame(summary, what="window_mismatch.csv")
        summary.to_csv(csv_path, index=False)
        _write_figure(summary, out_dir / "window_mismatch.png")
        print(f"wrote {csv_path}")
    else:
        frame = _mismatch_rows(labels, convention)
        summary = summarize_by_season(frame)
        overall = float(frame["outside_window"].mean()) if len(frame) else 0.0
        print(f"convention={convention} overall mismatch rate: {overall:.4%}")
        print(summary.to_string(index=False))
        csv_path = out_dir / "window_mismatch.csv"
        assert_non_empty_frame(summary, what="window_mismatch.csv")
        summary.to_csv(csv_path, index=False)
        _write_figure(summary, out_dir / "window_mismatch.png")
        print(f"wrote {csv_path}")

    return run_k2(config, labels, out_dir)


def _write_figure(summary: pd.DataFrame, path: Path) -> None:
    if summary.empty:
        return
    fig, ax = plt.subplots()
    for convention in sorted(summary["convention"].unique()):
        subset = summary[summary["convention"] == convention]
        ax.plot(subset["season"], subset["mismatch_rate"], marker="o", label=convention)
    ax.set_ylabel("mismatch rate")
    ax.set_xlabel("season")
    ax.set_title("CLI time-of-high outside NBM 12Z-06Z window")
    ax.legend()
    fig.savefig(path)
    plt.close(fig)
    print(f"wrote {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Window vs climate-day mismatch rate")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    default_out = (config.get("window_mismatch") or {}).get("out_dir") or "analysis/out"
    out_dir = args.out_dir or Path(default_out)
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
