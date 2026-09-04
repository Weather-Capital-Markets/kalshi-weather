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
from ingestion.climate_day import climate_date_of
from ingestion.climate_time import AsosObservation, asos_max_for_climate_day
from ingestion.config_loader import load_config
from ingestion.validate_units import assert_non_empty_rows

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

NOTES_CAVEAT = (
    "This measures IEM-ASOS-KLGA vs IEM-ASOS-KNYC. Polymarket settles on Weather "
    "Underground Daily Observations for KLGA, which may differ from IEM in ingest/QC/"
    "rounding. Per data-sources.md §1.3, this is a lower bound on true cross-venue "
    "basis: station-to-station difference only; provider-to-provider disagreement is "
    "unmeasured."
)

SEASONS = {
    "DJF": {12, 1, 2},
    "MAM": {3, 4, 5},
    "JJA": {6, 7, 8},
    "SON": {9, 10, 11},
}


def whole_f(tmpf: float) -> int:
    return int(round(tmpf))


def polymarket_bracket(max_f: int) -> str:
    """Assign a whole-°F daily max to Polymarket's even-edged bracket ladder."""
    if max_f <= 75:
        return "tail_below"
    if max_f >= 94:
        return "tail_above"
    low = max_f if max_f % 2 == 0 else max_f - 1
    return f"between_{low}-{low + 1}"


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
) -> dict[str, int]:
    """Return climate_date -> whole-°F daily max for days in [start, end]."""
    by_day: dict[str, list[AsosObservation]] = {}
    for obs in observations:
        climate = climate_date_of(obs.valid_utc)
        if start <= climate <= end:
            by_day.setdefault(climate.isoformat(), []).append(obs)

    maxes: dict[str, int] = {}
    current = start
    while current <= end:
        climate = current.isoformat()
        max_f, _ = asos_max_for_climate_day(by_day.get(climate, []), climate)
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
        "bracket_disagree_frac_middle": float("nan"),
        "knyc_middle_p10": float("nan"),
        "knyc_middle_p90": float("nan"),
    }
    return row


def add_middle_disagreement(
    summary_rows: list[dict[str, Any]],
    paired: pd.DataFrame,
) -> list[dict[str, Any]]:
    if paired.empty:
        return summary_rows
    p10 = float(paired["knyc_max_f"].quantile(0.10))
    p90 = float(paired["knyc_max_f"].quantile(0.90))
    middle = paired[(paired["knyc_max_f"] >= p10) & (paired["knyc_max_f"] <= p90)]
    middle_frac = float(middle["bracket_disagree"].mean()) if not middle.empty else float("nan")
    updated: list[dict[str, Any]] = []
    for row in summary_rows:
        new_row = dict(row)
        if row["slice"] == "overall":
            new_row["bracket_disagree_frac_middle"] = round(middle_frac, 4)
            new_row["knyc_middle_p10"] = round(p10, 1)
            new_row["knyc_middle_p90"] = round(p90, 1)
        updated.append(new_row)
    return updated


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    assert_non_empty_rows(len(rows), what="station_basis.csv summary rows")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# {NOTES_CAVEAT}\n")
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
    if season_rows.empty:
        return
    plt.figure(figsize=(7, 4))
    plt.bar(season_rows["slice"], season_rows["bracket_disagree_frac"])
    plt.ylabel("bracket_disagree_frac")
    plt.xlabel("season")
    plt.title("Different Polymarket bracket (even-edged ladder)")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
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
    paired = build_paired_days(
        knyc_maxes=knyc_maxes,
        klga_maxes=klga_maxes,
        start=start,
        end=end,
    )

    print("=== NOTES ===")
    print(NOTES_CAVEAT)
    print(f"window={start.isoformat()}..{end.isoformat()}")
    print(f"stations: KNYC={knyc_station}, KLGA={klga_station}")
    print("whole-°F daily max = round(IEM ASOS tmpf); LST climate day per data-sources.md §1.1")

    if paired.empty:
        print("no paired days with observations at both stations in range")
        return 0

    summary_rows = [summarize_slice(paired, slice_name="overall")]
    for season in ("DJF", "MAM", "JJA", "SON"):
        season_frame = paired[paired["season"] == season]
        summary_rows.append(summarize_slice(season_frame, slice_name=season))
    summary_rows = add_middle_disagreement(summary_rows, paired)

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
