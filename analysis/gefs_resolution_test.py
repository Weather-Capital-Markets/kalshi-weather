"""GEFS vs NBM vs market Murphy resolution on the K2 T−24h primary sample.

Allowed DB: none. Reads GEFS decoded parquet, k2_t24_tradable.csv, NBM
decoded_v441, markets JSONL, CLINYC. Frozen data only.

Prints distributions and Murphy components. No verdict language.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analysis.bracket_enumeration import ParsedStrike, parse_market_strike
from analysis.forecast_vs_market import settled_yes
from analysis.murphy import murphy_decompose
from analysis.spread_census import load_markets, season_of
from ingestion.asos_parse import load_asos_observations_from_raw
from ingestion.climate_day import climate_date_of, climate_day_end, climate_day_start
from ingestion.config_loader import load_config
from ingestion.gefs_archive import sample_climate_dates_from_nbm
from ingestion.gefs_idx import FORECAST_STEP_H, MEMBERS

plt.switch_backend("Agg")
logger = logging.getLogger(__name__)

N_BOOT = 2000
BOOT_SEED = 43
NOTE_TMP = (
    "NOTE: daily max is max of instantaneous 3-hourly 2 m TMP inside the LST "
    "climate day. Accumulating 0-N hour TMAX is not used."
)
NOTE_LAPLACE = (
    "NOTE: bracket probs are Laplace alpha=1, (n_k+1)/(N+K), locked before outcomes. "
    "Member Tmax is rounded to nearest int F to match CLI settlement."
)


def member_in_bracket(tmax_f: float, strike: ParsedStrike) -> bool:
    landed = settled_yes(int(round(float(tmax_f))), strike)
    return bool(landed)


def member_bracket_probabilities(
    tmaxes_f: list[float],
    strikes: list[ParsedStrike],
    *,
    alpha: float = 1.0,
) -> list[float]:
    """Empirical member frequency with Laplace α=1 over K day-brackets."""
    n_members = len(tmaxes_f)
    k = len(strikes)
    if n_members == 0 or k == 0:
        return [0.0] * k
    counts = [0.0] * k
    for tmax in tmaxes_f:
        for index, strike in enumerate(strikes):
            if member_in_bracket(tmax, strike):
                counts[index] += 1.0
                break
    denom = n_members + alpha * k
    return [(count + alpha) / denom for count in counts]


def load_gefs_day(decoded_dir: Path, climate_date: str) -> pd.DataFrame | None:
    path = decoded_dir / f"{climate_date}.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    return frame if not frame.empty else None


def bootstrap_resolution(
    yes: np.ndarray,
    pred: np.ndarray,
    *,
    n_draws: int = N_BOOT,
    seed: int = BOOT_SEED,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vals = np.empty(n_draws)
    n = len(yes)
    for draw in range(n_draws):
        idx = rng.integers(0, n, size=n)
        vals[draw] = murphy_decompose(yes[idx], pred[idx])["resolution"]
    return vals


def bootstrap_res_diff(
    yes: np.ndarray,
    gefs: np.ndarray,
    nbm: np.ndarray,
    *,
    n_draws: int = N_BOOT,
    seed: int = BOOT_SEED,
    clusters: np.ndarray | None = None,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_draws)
    n = len(yes)
    if clusters is None:
        for draw in range(n_draws):
            idx = rng.integers(0, n, size=n)
            diffs[draw] = (
                murphy_decompose(yes[idx], gefs[idx])["resolution"]
                - murphy_decompose(yes[idx], nbm[idx])["resolution"]
            )
        return diffs
    unique, inverse = np.unique(clusters, return_inverse=True)
    groups = [np.where(inverse == i)[0] for i in range(len(unique))]
    for draw in range(n_draws):
        sampled = rng.integers(0, len(unique), size=len(unique))
        idx = np.concatenate([groups[i] for i in sampled])
        diffs[draw] = (
            murphy_decompose(yes[idx], gefs[idx])["resolution"]
            - murphy_decompose(yes[idx], nbm[idx])["resolution"]
        )
    return diffs


def asos_three_hourly_bias(
    observations: list[Any],
    climate_dates: list[str],
) -> pd.DataFrame:
    """Hourly ASOS climate-day max minus max on GEFS-like 3-hourly valid times."""
    by_day: dict[str, list[Any]] = {}
    for obs in observations:
        by_day.setdefault(climate_date_of(obs.valid_utc).isoformat(), []).append(obs)
    rows: list[dict[str, Any]] = []
    for climate_date in climate_dates:
        day = pd.Timestamp(climate_date).date()
        start = climate_day_start(day)
        end = climate_day_end(day)
        obs = by_day.get(climate_date) or []
        in_window = [item for item in obs if start <= item.valid_utc < end]
        if not in_window:
            continue
        hourly_max = max(item.tmpf for item in in_window)
        three_h = [item.tmpf for item in in_window if item.valid_utc.hour % FORECAST_STEP_H == 0]
        if not three_h:
            continue
        three_max = max(three_h)
        rows.append(
            {
                "climate_date": climate_date,
                "hourly_max_f": hourly_max,
                "three_hourly_max_f": three_max,
                "bias_f": hourly_max - three_max,
            }
        )
    return pd.DataFrame(rows)


def _murphy_row(name: str, yes: np.ndarray, pred: np.ndarray) -> dict[str, Any]:
    out = murphy_decompose(yes, pred)
    out["model"] = name
    return out


def run_resolution_test(config: dict[str, Any]) -> int:
    storage = config["storage"]
    gefs_cfg = config.get("gefs_archive") or {}
    nbm_cfg = config.get("nbm_archive") or {}
    watch_cfg = config.get("gefs_availability_watch") or {}
    out_dir = Path(str(watch_cfg.get("out_dir") or "analysis/out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    gefs_dir = Path(str(gefs_cfg.get("decoded_dir") or "data/gefs/decoded"))
    nbm_dir = Path(
        str(gefs_cfg.get("nbm_sample_dir") or nbm_cfg.get("decoded_dir") or "data/nbm/decoded_v441")
    )
    trad_path = out_dir / "k2_t24_tradable.csv"
    print(NOTE_TMP)
    print(NOTE_LAPLACE)
    print(f"publication_latency_min={gefs_cfg.get('publication_latency_min')}")
    if not trad_path.exists():
        print("missing analysis/out/k2_t24_tradable.csv")
        return 1
    if not gefs_dir.exists() or not list(gefs_dir.glob("*.parquet")):
        print(f"gefs decoded dir empty: {gefs_dir} -- run gefs_archive backfill first")
        return 1

    tradable = pd.read_csv(trad_path)
    raw_dir = Path(storage["raw_dir"])
    markets = load_markets(raw_dir)
    markets_by_ticker = {str(m.get("ticker") or ""): m for m in markets}

    drop_reasons: dict[str, int] = {
        "no_gefs_parquet": 0,
        "member_count": 0,
        "no_settled_yes": 0,
        "no_nbm_prob": 0,
        "no_strike": 0,
        "kept": 0,
    }
    rows: list[dict[str, Any]] = []
    day_spread: list[dict[str, Any]] = []
    gefs_days = {path.stem for path in gefs_dir.glob("*.parquet")}

    for climate_date, group in tradable.groupby("climate_date"):
        climate_key = str(climate_date)
        if climate_key not in gefs_days:
            drop_reasons["no_gefs_parquet"] += len(group)
            continue
        gefs = load_gefs_day(gefs_dir, climate_key)
        if gefs is None or gefs["member"].nunique() != len(MEMBERS):
            drop_reasons["member_count"] += len(group)
            continue
        tmaxes = [float(v) for v in gefs["tmax_f"].tolist()]
        day_markets = []
        strikes: list[ParsedStrike] = []
        tickers: list[str] = []
        for ticker in group["ticker"].tolist():
            market = markets_by_ticker.get(str(ticker))
            strike = parse_market_strike(market) if market else None
            if strike is None:
                drop_reasons["no_strike"] += 1
                continue
            day_markets.append(market)
            strikes.append(strike)
            tickers.append(str(ticker))
        if not strikes:
            continue
        probs = member_bracket_probabilities(tmaxes, strikes)
        mean_tmax = float(np.mean(tmaxes))
        spread = float(np.std(tmaxes, ddof=1)) if len(tmaxes) > 1 else 0.0
        cli_high = None
        for _, trad_row in group.iterrows():
            ticker = str(trad_row["ticker"])
            if ticker not in tickers:
                continue
            if pd.isna(trad_row.get("settled_yes")):
                drop_reasons["no_settled_yes"] += 1
                continue
            if pd.isna(trad_row.get("nbm_prob")):
                drop_reasons["no_nbm_prob"] += 1
                continue
            index = tickers.index(ticker)
            if cli_high is None and "high_F" not in trad_row.index:
                cli_high = None
            rows.append(
                {
                    "climate_date": climate_key,
                    "season": season_of(climate_key),
                    "ticker": ticker,
                    "gefs_prob": probs[index],
                    "nbm_prob": float(trad_row["nbm_prob"]),
                    "market_mid_carryforward": float(trad_row["market_mid_carryforward"])
                    if pd.notna(trad_row["market_mid_carryforward"])
                    else float("nan"),
                    "combo_prob": 0.5 * float(trad_row["nbm_prob"]) + 0.5 * probs[index],
                    "settled_yes": float(trad_row["settled_yes"]),
                    "ensemble_mean_f": mean_tmax,
                    "ensemble_spread_f": spread,
                }
            )
            drop_reasons["kept"] += 1
        if rows:
            last_day = [r for r in rows if r["climate_date"] == climate_key]
            if last_day:
                day_spread.append(
                    {
                        "climate_date": climate_key,
                        "season": season_of(climate_key),
                        "ensemble_mean_f": mean_tmax,
                        "ensemble_spread_f": spread,
                    }
                )

    joined = pd.DataFrame(rows)
    print("join " + " ".join(f"{key}={value}" for key, value in drop_reasons.items()))
    if joined.empty:
        print("joined subset is empty")
        return 1

    yes = joined["settled_yes"].to_numpy(dtype=float)
    murphy_rows = [
        _murphy_row("gefs", yes, joined["gefs_prob"].to_numpy(dtype=float)),
        _murphy_row("nbm", yes, joined["nbm_prob"].to_numpy(dtype=float)),
        _murphy_row("market_cf", yes, joined["market_mid_carryforward"].to_numpy(dtype=float)),
        _murphy_row("nbm_gefs_equal", yes, joined["combo_prob"].to_numpy(dtype=float)),
    ]
    murphy = pd.DataFrame(murphy_rows)
    print("\n=== Murphy on joined subset ===")
    print(murphy.to_string(index=False))

    contract_diff = bootstrap_res_diff(
        yes,
        joined["gefs_prob"].to_numpy(dtype=float),
        joined["nbm_prob"].to_numpy(dtype=float),
    )
    day_diff = bootstrap_res_diff(
        yes,
        joined["gefs_prob"].to_numpy(dtype=float),
        joined["nbm_prob"].to_numpy(dtype=float),
        clusters=joined["climate_date"].to_numpy(),
    )
    print("\n=== bootstrap RES_GEFS - RES_NBM (2000, seed 43) ===")
    print(
        f"contract median={np.median(contract_diff):.6f} "
        f"p05={np.quantile(contract_diff, 0.05):.6f} "
        f"p95={np.quantile(contract_diff, 0.95):.6f}"
    )
    print(
        f"day_clustered median={np.median(day_diff):.6f} "
        f"p05={np.quantile(day_diff, 0.05):.6f} "
        f"p95={np.quantile(day_diff, 0.95):.6f}"
    )

    season_rows: list[dict[str, Any]] = []
    for season, group in joined.groupby("season"):
        yes_s = group["settled_yes"].to_numpy(dtype=float)
        season_rows.append(
            {
                "season": season,
                **{
                    f"{name}_{key}": value
                    for name, col in (
                        ("gefs", "gefs_prob"),
                        ("nbm", "nbm_prob"),
                        ("market", "market_mid_carryforward"),
                    )
                    for key, value in murphy_decompose(
                        yes_s, group[col].to_numpy(dtype=float)
                    ).items()
                    if key in {"n", "reliability", "resolution", "uncertainty", "brier"}
                },
            }
        )
        g_res = murphy_decompose(yes_s, group["gefs_prob"].to_numpy())["resolution"]
        n_res = murphy_decompose(yes_s, group["nbm_prob"].to_numpy())["resolution"]
        m_res = murphy_decompose(yes_s, group["market_mid_carryforward"].to_numpy())["resolution"]
        print(
            f"season={season} n={len(group)} "
            f"RES_gefs={g_res:.6f} RES_nbm={n_res:.6f} RES_mkt={m_res:.6f}"
        )

    labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
    cli = pd.read_csv(labels_csv) if labels_csv.exists() else pd.DataFrame()
    high_by_day: dict[str, float] = {}
    if not cli.empty and "climate_date" in cli.columns:
        high_col = "high_F" if "high_F" in cli.columns else None
        if high_col:
            for _, row in cli.iterrows():
                high_by_day[str(row["climate_date"])] = float(row[high_col])
    spread_frame = pd.DataFrame(day_spread)
    if not spread_frame.empty:
        spread_frame["cli_high_f"] = spread_frame["climate_date"].map(high_by_day)
        spread_frame["abs_error_f"] = (
            spread_frame["ensemble_mean_f"] - spread_frame["cli_high_f"]
        ).abs()
        valid = spread_frame.dropna(subset=["abs_error_f", "ensemble_spread_f"])
        if len(valid) >= 3:
            corr = float(valid["ensemble_spread_f"].corr(valid["abs_error_f"]))
            print(f"\nspread_vs_abs_error_corr={corr:.4f} n_days={len(valid)}")

    raw_dir = Path(storage["raw_dir"])
    asos = load_asos_observations_from_raw(raw_dir, station="NYC")
    sample_days = [d.isoformat() for d in sample_climate_dates_from_nbm(nbm_dir)]
    bias = asos_three_hourly_bias(asos, sample_days) if asos else pd.DataFrame()
    if not bias.empty:
        print(
            f"asos_3h_sampling_bias_median_f={bias['bias_f'].median():.3f} "
            f"mean={bias['bias_f'].mean():.3f} n={len(bias)}"
        )
        bias.to_csv(out_dir / "gefs_asos_3h_bias.csv", index=False)

    joined_path = out_dir / "gefs_resolution_test.csv"
    joined.to_csv(joined_path, index=False)
    murphy.to_csv(out_dir / "gefs_murphy_brier.csv", index=False)
    print(f"\nwrote {joined_path} rows={len(joined)}")

    fig, ax = plt.subplots(figsize=(7, 4))
    names = ["gefs", "nbm", "market_cf", "nbm_gefs_equal"]
    cols = {
        "gefs": "gefs_prob",
        "nbm": "nbm_prob",
        "market_cf": "market_mid_carryforward",
        "nbm_gefs_equal": "combo_prob",
    }
    res_vals = [float(murphy.loc[murphy["model"] == name, "resolution"].iloc[0]) for name in names]
    lo_err: list[float] = []
    hi_err: list[float] = []
    for name in names:
        draws = bootstrap_resolution(yes, joined[cols[name]].to_numpy(dtype=float))
        p05, p95 = float(np.quantile(draws, 0.05)), float(np.quantile(draws, 0.95))
        point = res_vals[names.index(name)]
        lo_err.append(max(0.0, point - p05))
        hi_err.append(max(0.0, p95 - point))
    ax.bar(names, res_vals, color="#4c6ef5", yerr=[lo_err, hi_err], capsize=4)
    ax.axhline(0, color="#111", linewidth=0.5)
    ax.set_ylabel("Murphy resolution")
    ax.set_title("Resolution on joined T−24h contracts (bars: 5–95% bootstrap)")
    fig.tight_layout()
    fig.savefig(out_dir / "gefs_resolution_comparison.png", dpi=120)
    plt.close(fig)

    if not spread_frame.empty and spread_frame["abs_error_f"].notna().any():
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.scatter(
            spread_frame["ensemble_spread_f"],
            spread_frame["abs_error_f"],
            s=12,
            alpha=0.7,
        )
        ax.set_xlabel("Ensemble spread of member Tmax (°F)")
        ax.set_ylabel("|ensemble mean − CLI high| (°F)")
        ax.set_title("GEFS spread vs absolute error")
        fig.tight_layout()
        fig.savefig(out_dir / "gefs_spread_vs_error.png", dpi=120)
        plt.close(fig)

    if season_rows:
        season_df = pd.DataFrame(season_rows)
        fig, ax = plt.subplots(figsize=(6, 4))
        seasons = season_df["season"].tolist()
        x = np.arange(len(seasons))
        width = 0.25
        ax.bar(x - width, season_df["gefs_resolution"], width, label="GEFS")
        ax.bar(x, season_df["nbm_resolution"], width, label="NBM")
        ax.bar(x + width, season_df["market_resolution"], width, label="market")
        ax.set_xticks(x)
        ax.set_xticklabels(seasons)
        ax.set_ylabel("Murphy resolution")
        ax.set_title("Resolution by season (joined subset)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "gefs_resolution_by_season.png", dpi=120)
        plt.close(fig)

    print(f"n_joined={len(joined)} n_days={joined['climate_date'].nunique()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GEFS resolution test (distributions only)")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return run_resolution_test(load_config(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
