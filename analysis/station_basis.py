"""KLGA–KNYC station basis measurement from IEM ASOS daily maxima.

Allowed DB: none. Reads asos_obs JSONL only.

Measurement output only — distributions and summary stats; no conclusion sentence.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import climate_date_of, climate_day_end, climate_day_start
from ingestion.climate_time import AsosObservation, asos_max_for_climate_day
from ingestion.config_loader import load_config

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

NOTES_CAVEAT = (
    "This measures IEM-ASOS-KLGA vs IEM-ASOS-KNYC. Polymarket settles on Weather "
    "Underground Daily Observations for KLGA, which may differ from IEM in ingest/QC/"
    "rounding. Per data-sources.md §1.3, this is a lower bound on true cross-venue "
    "basis: station-to-station difference only; provider-to-provider disagreement is "
    "unmeasured."
)
NOTE_SUMMER_LADDER = (
    "NOTE: polymarket_bracket() hard-codes one observed summer ladder (≤75 / ≥94). "
    "DJF/MAM/SON and overall bracket_disagree_frac are withdrawn_summer_ladder — "
    "winter maxes all land in tail_below, so disagreement is arithmetically impossible. "
    "Measured bracket rows are JJA and ladder_interior (both stations in 76–93°F)."
)

SEASONS = {
    "DJF": {12, 1, 2},
    "MAM": {3, 4, 5},
    "JJA": {6, 7, 8},
    "SON": {9, 10, 11},
}
LADDER_INTERIOR_LO = 76
LADDER_INTERIOR_HI = 93
COVERAGE_MIN_OBS = 12
BRACKET_MEASURED_SLICES = frozenset({"JJA", "ladder_interior"})


def whole_f(tmpf: float) -> int:
    return int(round(tmpf))


def polymarket_bracket(max_f: int) -> str:
    """Assign a whole-°F daily max to one observed Polymarket even-edged summer ladder.

    Cut points ≤75 / ≥94 are not a winter measurement. See NOTE_SUMMER_LADDER.
    """
    if max_f <= 75:
        return "tail_below"
    if max_f >= 94:
        return "tail_above"
    low = max_f if max_f % 2 == 0 else max_f - 1
    return f"between_{low}-{low + 1}"


def in_ladder_interior(max_f: int) -> bool:
    return LADDER_INTERIOR_LO <= max_f <= LADDER_INTERIOR_HI


def climate_day_obs_count(observations: list[AsosObservation], climate: str) -> int:
    day = date.fromisoformat(climate)
    start = climate_day_start(day)
    end = climate_day_end(day)
    return sum(1 for obs in observations if start <= obs.valid_utc < end)


def season_of(climate_date: str | date) -> str:
    day = date.fromisoformat(climate_date) if isinstance(climate_date, str) else climate_date
    month = day.month
    for name, months in SEASONS.items():
        if month in months:
            return name
    raise ValueError(f"unexpected month {month}")


def daily_max_table(
    observations: list[AsosObservation],
    *,
    start: date,
    end: date,
    min_obs: int = 1,
) -> dict[str, int]:
    """Return climate_date -> whole-°F daily max for days in [start, end].

    Days with fewer than min_obs surviving observations in the LST climate day
    are omitted. Default min_obs=1 keeps the historical inner-join n; run()
    reports how many days COVERAGE_MIN_OBS would drop.
    """
    by_day: dict[str, list[AsosObservation]] = {}
    for obs in observations:
        climate = climate_date_of(obs.valid_utc)
        if start <= climate <= end:
            by_day.setdefault(climate.isoformat(), []).append(obs)

    maxes: dict[str, int] = {}
    current = start
    while current <= end:
        climate = current.isoformat()
        day_obs = by_day.get(climate, [])
        if climate_day_obs_count(day_obs, climate) < min_obs:
            current += timedelta(days=1)
            continue
        max_f, _ = asos_max_for_climate_day(day_obs, climate)
        if max_f is not None:
            maxes[climate] = whole_f(max_f)
        current += timedelta(days=1)
    return maxes


def build_paired_days(
    *,
    knyc_maxes: dict[str, int],
    klga_maxes: dict[str, int],
    start: date,
    end: date,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    current = start
    while current <= end:
        climate = current.isoformat()
        if climate in knyc_maxes and climate in klga_maxes:
            knyc_f = knyc_maxes[climate]
            klga_f = klga_maxes[climate]
            delta = klga_f - knyc_f
            rows.append(
                {
                    "climate_date": climate,
                    "season": season_of(climate),
                    "month": current.month,
                    "knyc_max_f": knyc_f,
                    "klga_max_f": klga_f,
                    "delta_f": delta,
                    "knyc_bracket": polymarket_bracket(knyc_f),
                    "klga_bracket": polymarket_bracket(klga_f),
                    "bracket_disagree": polymarket_bracket(knyc_f) != polymarket_bracket(klga_f),
                    "ladder_interior": in_ladder_interior(knyc_f) and in_ladder_interior(klga_f),
                }
            )
        current += timedelta(days=1)
    return pd.DataFrame(rows)


def _quantile(series: pd.Series, q: float) -> float:
    clean = series.dropna()
    if clean.empty:
        return float("nan")
    return float(clean.quantile(q))


def summarize_slice(frame: pd.DataFrame, *, slice_name: str) -> dict[str, Any]:
    n = len(frame)
    delta = frame["delta_f"] if not frame.empty else pd.Series(dtype=float)
    disagree = frame["bracket_disagree"] if not frame.empty else pd.Series(dtype=bool)
    row: dict[str, Any] = {
        "slice": slice_name,
        "n_days": n,
        "delta_mean": round(float(delta.mean()), 3) if n else float("nan"),
        "delta_median": round(float(delta.median()), 3) if n else float("nan"),
        "delta_p10": round(_quantile(delta, 0.10), 3),
        "delta_p25": round(_quantile(delta, 0.25), 3),
        "delta_p75": round(_quantile(delta, 0.75), 3),
        "delta_p90": round(_quantile(delta, 0.90), 3),
        "share_delta_eq_0": round(float((delta == 0).mean()), 4) if n else float("nan"),
        "share_klga_warmer": round(float((delta > 0).mean()), 4) if n else float("nan"),
        "share_knyc_warmer": round(float((delta < 0).mean()), 4) if n else float("nan"),
        "bracket_disagree_frac": round(float(disagree.mean()), 4) if n else float("nan"),
        "bracket_status": "measured",
        "bracket_disagree_frac_middle": float("nan"),
        "knyc_middle_p10": float("nan"),
        "knyc_middle_p90": float("nan"),
    }
    return row


def withdraw_summer_ladder_brackets(row: dict[str, Any]) -> dict[str, Any]:
    """Null seasonal/overall bracket fractions that are not measurements."""
    out = dict(row)
    if out["slice"] in BRACKET_MEASURED_SLICES:
        out["bracket_status"] = "measured"
        return out
    out["bracket_status"] = "withdrawn_summer_ladder"
    out["bracket_disagree_frac"] = float("nan")
    out["bracket_disagree_frac_middle"] = float("nan")
    return out


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTES_CAVEAT}\n")
        handle.write(f"# {NOTE_SUMMER_LADDER}\n")
        handle.write(
            "# whole-°F daily max = round(IEM ASOS tmpf); "
            "LST climate day per data-sources.md §1.1\n"
        )
    pd.DataFrame(rows).to_csv(path, mode="a", index=False)


def _plot_delta_hist(frame: pd.DataFrame, path: Path) -> None:
    if frame.empty:
        return
    delta_min = int(frame["delta_f"].min())
    delta_max = int(frame["delta_f"].max())
    bins = range(delta_min - 1, delta_max + 2)
    plt.figure(figsize=(8, 4))
    plt.hist(frame["delta_f"], bins=bins)
    plt.xlabel("delta_f = round(KLGA max) - round(KNYC max)")
    plt.ylabel("climate days")
    plt.title("KLGA - KNYC daily max (whole °F)")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()


def _plot_disagree_by_season(summary: pd.DataFrame, path: Path) -> None:
    season_rows = summary[summary["slice"].isin(SEASONS.keys())]
    if "bracket_status" in summary.columns:
        season_rows = season_rows[season_rows["bracket_status"] == "measured"]
    season_rows = season_rows[season_rows["n_days"] > 0]
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(7, 4))
    if season_rows.empty:
        plt.title("Measured seasonal bracket disagreement (none in this window)")
    else:
        plt.bar(season_rows["slice"], season_rows["bracket_disagree_frac"])
        plt.ylabel("bracket_disagree_frac")
        plt.xlabel("season")
        plt.title("Different Polymarket bracket (JJA / interior only)")
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def _plot_delta_by_month(frame: pd.DataFrame, path: Path) -> None:
    if frame.empty:
        return
    grouped = [frame.loc[frame["month"] == month, "delta_f"].tolist() for month in range(1, 13)]
    plt.figure(figsize=(10, 4))
    plt.boxplot(grouped, tick_labels=[str(m) for m in range(1, 13)])
    plt.xlabel("month")
    plt.ylabel("delta_f")
    plt.title("KLGA - KNYC by month of year")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()


def disagree_by_knyc_max(frame: pd.DataFrame) -> pd.DataFrame:
    """Bracket disagreement rate conditional on KNYC whole-°F daily max."""
    if frame.empty:
        return pd.DataFrame(
            columns=["knyc_max_f", "n_days", "bracket_disagree_frac", "delta_median"]
        )
    grouped = (
        frame.groupby("knyc_max_f", as_index=False)
        .agg(
            n_days=("bracket_disagree", "size"),
            bracket_disagree_frac=("bracket_disagree", "mean"),
            delta_median=("delta_f", "median"),
        )
        .sort_values("knyc_max_f")
    )
    grouped["bracket_disagree_frac"] = grouped["bracket_disagree_frac"].round(4)
    grouped["delta_median"] = grouped["delta_median"].round(3)
    return grouped


def write_disagree_by_knyc_csv(path: Path, table: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "# bracket_disagree_frac conditional on KNYC whole-°F daily max; "
            "same IEM-vs-WU lower-bound caveat as station_basis.csv\n"
        )
    table.to_csv(path, mode="a", index=False)


def _plot_disagree_by_knyc_max(
    table: pd.DataFrame,
    path: Path,
    *,
    title: str,
    min_days: int = 5,
) -> None:
    if table.empty:
        return
    plot = table[table["n_days"] >= min_days].copy()
    if plot.empty:
        plot = table.copy()
    plt.figure(figsize=(11, 4))
    plt.bar(plot["knyc_max_f"], plot["bracket_disagree_frac"], width=0.8)
    plt.xlabel("KNYC daily max (whole °F)")
    plt.ylabel("bracket_disagree_frac")
    plt.title(title)
    plt.ylim(0, 1)
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()


def _print_disagree_by_knyc(table: pd.DataFrame) -> None:
    if table.empty:
        return
    print("\n=== bracket disagreement by KNYC max (whole °F) ===")
    eligible = table[table["n_days"] >= 10].copy()
    if eligible.empty:
        eligible = table.copy()
    top = eligible.sort_values("bracket_disagree_frac", ascending=False).head(8)
    for row in top.itertuples(index=False):
        print(
            f"knyc_max_f={int(row.knyc_max_f):>3} "
            f"n={int(row.n_days):>4} "
            f"disagree_frac={row.bracket_disagree_frac:.4f} "
            f"delta_median={row.delta_median:.1f}"
        )


def _print_slice(row: dict[str, Any]) -> None:
    print(
        f"\n=== {row['slice']} (n={row['n_days']}) ===\n"
        f"delta_f: mean={row['delta_mean']}, median={row['delta_median']}, "
        f"p10={row['delta_p10']}, p25={row['delta_p25']}, "
        f"p75={row['delta_p75']}, p90={row['delta_p90']}\n"
        f"share_delta_eq_0={row['share_delta_eq_0']}, "
        f"share_klga_warmer={row['share_klga_warmer']}, "
        f"share_knyc_warmer={row['share_knyc_warmer']}\n"
        f"bracket_disagree_frac={row['bracket_disagree_frac']}"
    )
    status = row.get("bracket_status")
    if status and status != "measured":
        print(f"bracket_status={status}")
    if row["slice"] == "overall" and not pd.isna(row.get("knyc_middle_p10")):
        print(
            f"knyc middle band: [{row['knyc_middle_p10']}, {row['knyc_middle_p90']}] °F\n"
            f"bracket_disagree_frac_middle={row['bracket_disagree_frac_middle']}"
        )


def run(config: dict[str, Any], out_dir: Path) -> int:
    storage = config["storage"]
    raw_dir = Path(storage["raw_dir"])
    basis_cfg = config.get("station_basis") or {}
    asos_cfg = config.get("asos_obs") or {}
    start = date.fromisoformat(
        str(basis_cfg.get("start_date") or asos_cfg.get("start_date") or "2021-01-01")
    )
    end = date.today()
    knyc_station = str(basis_cfg.get("knyc_station") or "NYC")
    klga_station = str(basis_cfg.get("klga_station") or "LGA")

    knyc_obs = load_asos_observations_from_raw(raw_dir, station=knyc_station)
    klga_obs = load_asos_observations_from_raw(raw_dir, station=klga_station)

    knyc_maxes = daily_max_table(knyc_obs, start=start, end=end)
    klga_maxes = daily_max_table(klga_obs, start=start, end=end)
    knyc_cov = daily_max_table(knyc_obs, start=start, end=end, min_obs=COVERAGE_MIN_OBS)
    klga_cov = daily_max_table(klga_obs, start=start, end=end, min_obs=COVERAGE_MIN_OBS)
    paired = build_paired_days(
        knyc_maxes=knyc_maxes,
        klga_maxes=klga_maxes,
        start=start,
        end=end,
    )
    paired_cov = build_paired_days(
        knyc_maxes=knyc_cov,
        klga_maxes=klga_cov,
        start=start,
        end=end,
    )

    print("=== NOTES ===")
    print(NOTES_CAVEAT)
    print(NOTE_SUMMER_LADDER)
    print(f"window={start.isoformat()}..{end.isoformat()}")
    print(f"stations: KNYC={knyc_station}, KLGA={klga_station}")
    print("whole-°F daily max = round(IEM ASOS tmpf); LST climate day per data-sources.md §1.1")
    print(
        f"coverage knyc_days={len(knyc_maxes)} knyc_min_obs_{COVERAGE_MIN_OBS}={len(knyc_cov)} "
        f"dropped={len(knyc_maxes) - len(knyc_cov)}; "
        f"klga_days={len(klga_maxes)} klga_min_obs_{COVERAGE_MIN_OBS}={len(klga_cov)} "
        f"dropped={len(klga_maxes) - len(klga_cov)}; "
        f"paired={len(paired)} paired_min_obs_{COVERAGE_MIN_OBS}={len(paired_cov)} "
        f"dropped={len(paired) - len(paired_cov)}"
    )

    if paired.empty:
        print("no paired days with observations at both stations in range")
        return 0

    summary_rows = [summarize_slice(paired, slice_name="overall")]
    for season in ("DJF", "MAM", "JJA", "SON"):
        season_frame = paired[paired["season"] == season]
        summary_rows.append(summarize_slice(season_frame, slice_name=season))
    if "ladder_interior" in paired.columns:
        interior = paired[paired["ladder_interior"]]
    else:
        interior = paired.iloc[0:0]
    summary_rows.append(summarize_slice(interior, slice_name="ladder_interior"))
    summary_rows.append(summarize_slice(paired_cov, slice_name="coverage_min_obs_12"))
    summary_rows = [withdraw_summer_ladder_brackets(row) for row in summary_rows]

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "station_basis.csv"
    write_summary_csv(csv_path, summary_rows)
    print(f"\nsummary csv: {csv_path}")

    hist_path = out_dir / "station_basis_delta_hist.png"
    season_path = out_dir / "station_basis_disagree_by_season.png"
    month_path = out_dir / "station_basis_delta_by_month.png"
    _plot_delta_hist(paired, hist_path)
    _plot_disagree_by_season(pd.DataFrame(summary_rows), season_path)
    _plot_delta_by_month(paired, month_path)

    by_knyc = disagree_by_knyc_max(paired)
    by_knyc_path = out_dir / "station_basis_disagree_by_knyc_max.csv"
    by_knyc_png = out_dir / "station_basis_disagree_by_knyc_max.png"
    by_knyc_jja_png = out_dir / "station_basis_disagree_by_knyc_max_jja.png"
    write_disagree_by_knyc_csv(by_knyc_path, by_knyc)
    _plot_disagree_by_knyc_max(
        by_knyc,
        by_knyc_png,
        title="Bracket disagreement rate by KNYC daily max (all seasons)",
    )
    jja = paired[paired["season"] == "JJA"]
    by_knyc_jja = disagree_by_knyc_max(jja)
    _plot_disagree_by_knyc_max(
        by_knyc_jja,
        by_knyc_jja_png,
        title="Bracket disagreement rate by KNYC daily max (JJA only)",
        min_days=3,
    )

    print(f"histogram delta_f: {hist_path}")
    print(f"bracket disagreement by season: {season_path}")
    print(f"delta_f by month: {month_path}")
    print(f"disagreement by KNYC max csv: {by_knyc_path}")
    print(f"disagreement by KNYC max plot: {by_knyc_png}")
    print(f"disagreement by KNYC max plot (JJA): {by_knyc_jja_png}")
    _print_disagree_by_knyc(by_knyc)
    _print_disagree_by_knyc(by_knyc_jja)

    for row in summary_rows:
        _print_slice(row)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KLGA–KNYC station basis measurement")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config(args.config)
    basis_cfg = config.get("station_basis") or {}
    out_dir = args.out_dir or Path(str(basis_cfg.get("out_dir") or "analysis/out"))
    return run(config, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
