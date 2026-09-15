"""Independent recompute of K2 rigor numbers. Read-only. No new extracts."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.k2_rigor import quadratic_taker_fee
from analysis.murphy import (
    day_clustered_brier_diff,
    murphy_decompose,
    paired_brier_diff,
)
from ingestion.config_loader import load_config
from ingestion.nbm_archive import NbmArchiveBackfill

OUT = Path("analysis/out")
T24 = Path("data/nbm/decoded_v441")
T12 = Path("data/nbm/decoded_v441_t12")
P99 = Path("data/nbm/decoded_v441_p99")


def brier(yes: np.ndarray, pred: np.ndarray) -> float:
    return float(((pred - yes) ** 2).mean())


def day_clustered_diff(
    frame: pd.DataFrame,
    yes_col: str,
    a_col: str,
    b_col: str,
    *,
    n_boot: int = 2000,
    seed: int = 43,
) -> dict[str, float]:
    """Bootstrap days, not contracts. Each day contributes its mean SE difference."""
    return day_clustered_brier_diff(
        frame,
        a_col,
        b_col,
        yes_col=yes_col,
        n_boot=n_boot,
        seed=seed,
    )


def main() -> None:
    findings: list[str] = []

    def note(text: str) -> None:
        findings.append(text)
        print(text)

    trad = pd.read_csv(OUT / "k2_t24_tradable.csv")
    t12 = pd.read_csv(OUT / "k2_t12_vs_t24.csv")
    books = pd.read_csv(OUT / "k2_horizon_books.csv")
    pit = pd.read_csv(OUT / "k2_pit_days.csv")
    win = pd.read_csv(OUT / "k2_pit_window_asos.csv")
    murphy = pd.read_csv(OUT / "k2_murphy_brier.csv")
    fee = pd.read_csv(OUT / "k2_taker_after_fee.csv")
    terc = pd.read_csv(OUT / "k2_taker_tercile_width.csv")
    p99_days = pd.read_csv(OUT / "k2_p99_days.csv")
    p99_br = pd.read_csv(OUT / "k2_p99_brackets.csv")
    rev = pd.read_csv(OUT / "k2_cli_revisions.csv")
    t12_sum = pd.read_csv(OUT / "k2_t12_summary.csv")
    p99_sum = pd.read_csv(OUT / "k2_p99_summary.csv")
    note(f"p99_summary_rows={len(p99_sum)}")

    cfg = load_config()
    sample = NbmArchiveBackfill(cfg).sampled_climate_dates()
    sample_s = {d.isoformat() for d in sample}
    t24_files = {p.stem for p in T24.glob("*.parquet")}
    t12_files = {p.stem for p in T12.glob("*.parquet")}
    p99_files = {p.stem for p in P99.glob("*.parquet")}
    note(f"sample n={len(sample)} seed={cfg['nbm_archive']['sample_seed']}")
    note(f"t24_parquet={len(t24_files)} match_sample={t24_files == sample_s}")
    note(f"t12_parquet={len(t12_files)} match_sample={t12_files == sample_s}")
    note(f"p99_parquet={len(p99_files)} subset_of_sample={p99_files <= sample_s}")
    if t24_files != sample_s:
        note(f"FAIL t24 vs sample extra={t24_files - sample_s} missing={sample_s - t24_files}")
    if t12_files != sample_s:
        note(f"FAIL t12 vs sample extra={t12_files - sample_s} missing={sample_s - t12_files}")

    # Vintage metadata
    t24_meta = pd.read_parquet(T24 / "2022-12-15.parquet")
    t12_meta = pd.read_parquet(T12 / "2022-12-15.parquet")
    note(
        f"vintage T-24 2022-12-15 fh={int(t24_meta['forecast_hour'].iloc[0])} "
        f"cycle={t24_meta['vintage_cycle_utc'].iloc[0]} "
        f"lead={t24_meta['forecast_lead_h'].iloc[0]} nlev={len(t24_meta)}"
    )
    note(
        f"vintage T-12 2022-12-15 fh={int(t12_meta['forecast_hour'].iloc[0])} "
        f"cycle={t12_meta['vintage_cycle_utc'].iloc[0]} "
        f"lead={t12_meta['forecast_lead_h'].iloc[0]} nlev={len(t12_meta)}"
    )
    t12_fh = []
    t12_cyc_hour = []
    for stem in sorted(t12_files)[:20]:
        d = pd.read_parquet(T12 / f"{stem}.parquet")
        t12_fh.append(int(d["forecast_hour"].iloc[0]))
        t12_cyc_hour.append(str(d["vintage_cycle_utc"].iloc[0]))
    note(f"t12 first20 fh unique={set(t12_fh)}")
    bad_t12 = []
    for stem in sorted(t12_files):
        d = pd.read_parquet(T12 / f"{stem}.parquet")
        fh = int(d["forecast_hour"].iloc[0])
        cyc = str(d["vintage_cycle_utc"].iloc[0])
        if fh != 30 or "T00:00" not in cyc:
            bad_t12.append((stem, fh, cyc))
    note(f"t12 vintage anomalies (fh!=30 or not 00Z) n={len(bad_t12)} examples={bad_t12[:5]}")
    bad_t24 = []
    for stem in sorted(t24_files):
        d = pd.read_parquet(T24 / f"{stem}.parquet")
        fh = int(d["forecast_hour"].iloc[0])
        cyc = str(d["vintage_cycle_utc"].iloc[0])
        if fh != 42 or "T12:00" not in cyc:
            bad_t24.append((stem, fh, cyc))
    note(f"t24 vintage anomalies (fh!=42 or not 12Z) n={len(bad_t24)} examples={bad_t24[:5]}")

    # T-24 Brier recompute
    t24_s = trad[
        trad["settled_yes"].notna()
        & trad["nbm_prob"].notna()
        & trad["market_mid_carryforward"].notna()
    ]
    yes = t24_s["settled_yes"].astype(float).to_numpy()
    nbm = t24_s["nbm_prob"].astype(float).to_numpy()
    mkt = t24_s["market_mid_carryforward"].astype(float).to_numpy()
    note(
        f"tradable rows={len(trad)} settled={len(t24_s)} in_primary={trad['in_primary_band'].all()}"
    )
    note(
        f"recompute T-24 Brier nbm={brier(yes, nbm):.10f} mkt={brier(yes, mkt):.10f} "
        f"gap={brier(yes, nbm) - brier(yes, mkt):.10f}"
    )
    csv_row = t12_sum[t12_sum["slice"] == "t24_primary"].iloc[0]
    note(
        f"csv T-24 n={int(csv_row.n)} nbm={csv_row.nbm_brier:.10f} mkt={csv_row.market_brier:.10f} "
        f"match_nbm={np.isclose(csv_row.nbm_brier, brier(yes, nbm))} "
        f"match_mkt={np.isclose(csv_row.market_brier, brier(yes, mkt))}"
    )
    contract = paired_brier_diff(yes, nbm, mkt)
    clustered = day_clustered_diff(t24_s, "settled_yes", "nbm_prob", "market_mid_carryforward")
    note(
        f"T-24 gap contract-boot mean={contract['mean']:.6f} "
        f"CI=[{contract['ci_lo']:.6f},{contract['ci_hi']:.6f}] n_contracts={int(contract['n'])}"
    )
    note(
        f"T-24 gap DAY-clustered mean={clustered['mean']:.6f} "
        f"CI=[{clustered['ci_lo']:.6f},{clustered['ci_hi']:.6f}] n_days={int(clustered['n'])}"
    )
    note(f"frac_nbm_better={contract['frac_a_better']:.4f} (share of contracts, not mean Brier)")

    for season in ("DJF", "JJA", "MAM", "SON"):
        g = t24_s[t24_s["season"] == season]
        gy = g["settled_yes"].astype(float).to_numpy()
        gn = g["nbm_prob"].astype(float).to_numpy()
        gm = g["market_mid_carryforward"].astype(float).to_numpy()
        c = paired_brier_diff(gy, gn, gm)
        d = day_clustered_diff(g, "settled_yes", "nbm_prob", "market_mid_carryforward")
        csv = t12_sum[t12_sum["slice"] == f"t24_primary_{season}"].iloc[0]
        note(
            f"T-24 {season} n={len(g)} gap={c['mean']:.4f} "
            f"contractCI=[{c['ci_lo']:+.4f},{c['ci_hi']:+.4f}] "
            f"dayCI=[{d['ci_lo']:+.4f},{d['ci_hi']:+.4f}] "
            f"csv_match={np.isclose(csv.nbm_minus_market, c['mean'])}"
        )

    # T-12
    note(
        f"t12 csv rows={len(t12)} settled={t12['settled_yes'].notna().sum()} "
        f"days={t12['climate_date'].nunique()}"
    )
    t12_s = t12[
        t12["settled_yes"].notna()
        & t12["nbm_prob_t24"].notna()
        & t12["nbm_prob_t12"].notna()
        & t12["market_mid"].notna()
    ]
    y12 = t12_s["settled_yes"].astype(float).to_numpy()
    a24 = t12_s["nbm_prob_t24"].astype(float).to_numpy()
    a12 = t12_s["nbm_prob_t12"].astype(float).to_numpy()
    m12 = t12_s["market_mid"].astype(float).to_numpy()
    note(
        f"T-12 settled n={len(t12_s)} "
        f"brier_t24={brier(y12, a24):.6f} brier_t12={brier(y12, a12):.6f} "
        f"brier_mkt={brier(y12, m12):.6f}"
    )
    v24 = paired_brier_diff(y12, a24, m12)
    v12 = paired_brier_diff(y12, a12, m12)
    vvt = paired_brier_diff(y12, a24, a12)
    d24 = day_clustered_diff(t12_s, "settled_yes", "nbm_prob_t24", "market_mid")
    d12 = day_clustered_diff(t12_s, "settled_yes", "nbm_prob_t12", "market_mid")
    dvt = day_clustered_diff(t12_s, "settled_yes", "nbm_prob_t24", "nbm_prob_t12")
    note(
        f"T-12 frozenNBM-mkt contractCI=[{v24['ci_lo']:+.4f},{v24['ci_hi']:+.4f}] "
        f"dayCI=[{d24['ci_lo']:+.4f},{d24['ci_hi']:+.4f}]"
    )
    note(
        f"T-12 laterNBM-mkt contractCI=[{v12['ci_lo']:+.4f},{v12['ci_hi']:+.4f}] "
        f"dayCI=[{d12['ci_lo']:+.4f},{d12['ci_hi']:+.4f}]"
    )
    note(
        f"T-12 frozen-later NBM contractCI=[{vvt['ci_lo']:+.4f},{vvt['ci_hi']:+.4f}] "
        f"dayCI=[{dvt['ci_lo']:+.4f},{dvt['ci_hi']:+.4f}]"
    )

    # Join integrity: nbm_prob_t24 vs horizon books
    t12_books = books[(books["horizon_h"] == 12) & books["in_primary_band"]].copy()
    merged = t12_s.merge(
        t12_books[["climate_date", "ticker", "nbm_prob", "market_mid_carryforward"]],
        on=["climate_date", "ticker"],
        how="left",
        suffixes=("", "_book"),
    )
    nbm_join_err = (merged["nbm_prob_t24"] - merged["nbm_prob"]).abs().max()
    mid_join_err = (merged["market_mid"] - merged["market_mid_carryforward"]).abs().max()
    note(f"join T-12 vs books max|nbm_t24-book|={nbm_join_err} max|mid-book|={mid_join_err}")

    # Selection: T-24 Brier on the SAME 692 contracts
    keys = t12_s[["climate_date", "ticker"]]
    t24_on_t12 = trad.merge(keys, on=["climate_date", "ticker"], how="inner")
    t24_on_t12 = t24_on_t12[t24_on_t12["settled_yes"].notna()]
    note(f"T-24 tradable overlap with T-12-band settled n={len(t24_on_t12)} (expect ~692)")
    if len(t24_on_t12):
        yo = t24_on_t12["settled_yes"].astype(float).to_numpy()
        no = t24_on_t12["nbm_prob"].astype(float).to_numpy()
        mo = t24_on_t12["market_mid_carryforward"].astype(float).to_numpy()
        note(
            f"T-24 Brier ON T-12-band contracts nbm={brier(yo, no):.6f} "
            f"mkt={brier(yo, mo):.6f} gap={brier(yo, no) - brier(yo, mo):.6f} "
            f"(vs full T-24 primary gap {brier(yes, nbm) - brier(yes, mkt):.6f})"
        )

    # NBM prob location on T-12 band (selection)
    note(
        f"T-12 band nbm_t24 mean={t12_s['nbm_prob_t24'].mean():.3f} "
        f"frac_outside_10_90="
        f"{((t12_s['nbm_prob_t24'] < 0.10) | (t12_s['nbm_prob_t24'] > 0.90)).mean():.3f} "
        f"nbm_t12 frac_out="
        f"{((t12_s['nbm_prob_t12'] < 0.10) | (t12_s['nbm_prob_t12'] > 0.90)).mean():.3f} "
        f"mkt always in band by construction"
    )
    note(
        f"median edge all-rows t24={t12['edge_t24'].median():.4f} "
        f"t12={t12['edge_t12'].median():.4f} "
        f"settled-only t24={t12_s['edge_t24'].median():.4f} "
        f"t12={t12_s['edge_t12'].median():.4f} "
        f"|dNBM| all={ (t12['nbm_prob_t12']-t12['nbm_prob_t24']).abs().median():.4f} "
        f"settled={ (t12_s['nbm_prob_t12']-t12_s['nbm_prob_t24']).abs().median():.4f}"
    )

    # JJA composition: T-24 JJA on contracts that survive to T-12 JJA
    jja12 = t12_s[t12_s["season"] == "JJA"]
    jja24_all = t24_s[t24_s["season"] == "JJA"]
    jja24_matched = jja24_all.merge(
        jja12[["climate_date", "ticker"]], on=["climate_date", "ticker"]
    )
    jja_yes = jja24_all["settled_yes"].astype(float).to_numpy()
    jja_nbm = jja24_all["nbm_prob"].astype(float).to_numpy()
    jja_mkt = jja24_all["market_mid_carryforward"].astype(float).to_numpy()
    note(
        f"JJA T-24 all n={len(jja24_all)} "
        f"gap={brier(jja_yes, jja_nbm) - brier(jja_yes, jja_mkt):.4f}"
    )
    if len(jja24_matched):
        jy = jja24_matched["settled_yes"].astype(float).to_numpy()
        jn = jja24_matched["nbm_prob"].astype(float).to_numpy()
        jm = jja24_matched["market_mid_carryforward"].astype(float).to_numpy()
        j12_yes = jja12["settled_yes"].astype(float).to_numpy()
        j12_nbm = jja12["nbm_prob_t12"].astype(float).to_numpy()
        j12_mkt = jja12["market_mid"].astype(float).to_numpy()
        note(
            f"JJA T-24 ON T-12-surviving contracts n={len(jja24_matched)} "
            f"gap={brier(jy, jn) - brier(jy, jm):.4f} "
            f"vs T-12 laterNBM-mkt gap={brier(j12_yes, j12_nbm) - brier(j12_yes, j12_mkt):.4f}"
        )

    # Murphy
    m_nbm = murphy_decompose(yes, nbm)
    m_mkt = murphy_decompose(yes, mkt)
    note(
        f"Murphy recompute nbm brier={m_nbm['brier']:.6f} REL={m_nbm['reliability']:.6f} "
        f"RES={m_nbm['resolution']:.6f} UNC={m_nbm['uncertainty']:.6f} "
        f"recon={m_nbm['reconstructed']:.6f} "
        f"recon-brier={m_nbm['reconstructed'] - m_nbm['brier']:.6f}"
    )
    note(
        f"Murphy recompute mkt brier={m_mkt['brier']:.6f} REL={m_mkt['reliability']:.6f} "
        f"RES={m_mkt['resolution']:.6f} recon-brier={m_mkt['reconstructed']-m_mkt['brier']:.6f}"
    )
    csv_m = murphy.set_index("model")
    note(
        f"Murphy csv match nbm_brier={np.isclose(csv_m.loc['nbm','brier'], m_nbm['brier'])} "
        f"mkt_brier={np.isclose(csv_m.loc['market_cf','brier'], m_mkt['brier'])}"
    )

    # Window PIT
    overlap = (
        win["win_below_p10"].astype(float)
        + win["win_in_p10_p90"].astype(float)
        + win["win_above_p90"].astype(float)
    )
    note(
        f"window PIT n={len(win)} in={win['win_in_p10_p90'].mean():.6f} "
        f"below={win['win_below_p10'].mean():.6f} "
        f"above={win['win_above_p90'].mean():.6f} "
        f"bias={win['win_high_minus_p50'].mean():.6f} "
        f"mae={win['win_high_minus_p50'].abs().mean():.6f} "
        f"exclusive_sum_eq1={(overlap == 1).all()} max_sum={overlap.max()}"
    )
    jja = win[win["season"] == "JJA"]
    note(
        f"JJA window n={len(jja)} above_p90={jja['win_above_p90'].mean()} "
        f"below={jja['win_below_p10'].mean()} bias={jja['win_high_minus_p50'].mean():.6f} "
        f"n_above={int(jja['win_above_p90'].sum())}"
    )
    # Binomial interval on coverage
    n_win = len(win)
    k_in = int(win["win_in_p10_p90"].sum())
    p_hat = k_in / n_win
    se = math.sqrt(p_hat * (1 - p_hat) / n_win)
    note(
        f"window in-band {k_in}/{n_win}={p_hat:.4f} "
        f"Wald95%=[{p_hat - 1.96 * se:.4f},{p_hat + 1.96 * se:.4f}] vs 0.80"
    )

    # Fee identity
    fee2 = fee.dropna(subset=["bid", "taker_sell_yes_edge"]).copy()
    recon_fee = fee2["bid"].map(quadratic_taker_fee)
    fee_recon_err = (fee2["fee_sell"] - recon_fee).abs().max()
    after_err = (
        (fee2["taker_sell_after_fee"] - (fee2["taker_sell_yes_edge"] - fee2["fee_sell"]))
        .abs()
        .max()
    )
    note(
        f"fee n={len(fee)} recon max|fee_sell-formula|={fee_recon_err} "
        f"max|after-(edge-fee)|={after_err} "
        f"median_after={fee['taker_sell_after_fee'].median():.6f} "
        f"DJF={fee.loc[fee.season=='DJF','taker_sell_after_fee'].median():.6f} "
        f"JJA={fee.loc[fee.season=='JJA','taker_sell_after_fee'].median():.6f}"
    )
    half = quadratic_taker_fee(0.5)
    note(f"fee at 50c={half} expect 0.0175")

    # Tercile n sum
    nbm_terc = terc[terc["slice"].str.startswith("nbm_")]
    note(f"tercile nbm n_sum={nbm_terc['n'].sum()} vs tradable {len(trad)}")
    high = terc[terc["slice"] == "nbm_high"].iloc[0]
    low = terc[terc["slice"] == "nbm_low"].iloc[0]
    note(
        f"nbm_high median_taker_sell={high['median_taker_sell']:.6f} "
        f"nbm_low={low['median_taker_sell']:.6f}"
    )

    # CLI revisions
    for era, g in rev.groupby("era"):
        if era == "first_7_or_8am":
            note(
                f"CLI {era} n={len(g)} d07={int(g['snap_07_disagree'].fillna(False).sum())} "
                f"d08={int(g['snap_08_disagree'].fillna(False).sum())}"
            )
        else:
            note(f"CLI {era} n={len(g)} d10={int(g['snap_10_disagree'].fillna(False).sum())}")

    # 99-level
    note(
        f"p99 days={len(p99_days)} levels_ok={(p99_days['n_levels_p99']==99).all()} "
        f"median subsample MAE={p99_days['mae_decile_vs_subsample'].median()} "
        f"max subsample MAE={p99_days['mae_decile_vs_subsample'].max()} "
        f"median p99 MAE={p99_days['mae_decile_vs_p99'].median():.6f}"
    )
    p50_err = (p99_days["nbm9_nbm_p50_f"] - p99_days["p50_nearest"]).abs()
    note(f"P50 9-decile vs p99 nearest median|d|={p50_err.median():.6e} max={p50_err.max():.6e}")
    iso = 0
    iso_p50 = 0
    for stem in p99_files:
        d = pd.read_parquet(P99 / f"{stem}.parquet")
        if bool(d["isotonic_adjusted"].any()):
            iso += 1
        row50 = d[d["percentile_level"] == 50]
        if len(row50) and bool(row50["isotonic_adjusted"].iloc[0]):
            iso_p50 += 1
    note(f"p99 days with any isotonic={iso}/50 days with P50 isotonic={iso_p50}/50")

    br_s = p99_br[
        p99_br["settled_yes"].notna()
        & p99_br["nbm_prob_decile"].notna()
        & p99_br["nbm_prob_p99"].notna()
    ]
    band = p99_br["in_primary_band"].fillna(False).astype(bool)
    note(
        f"p99 brackets rows={len(p99_br)} "
        f"in_primary_dtype={p99_br['in_primary_band'].dtype} "
        f"band_true={int(band.sum())}"
    )
    primary = p99_br[band]
    primary_s = primary[
        primary["settled_yes"].notna()
        & primary["nbm_prob_decile"].notna()
        & primary["nbm_prob_p99"].notna()
    ]
    note(f"p99 primary settled n={len(primary_s)}")
    if len(primary_s):
        yb = primary_s["settled_yes"].astype(float).to_numpy()
        d9 = primary_s["nbm_prob_decile"].astype(float).to_numpy()
        d99 = primary_s["nbm_prob_p99"].astype(float).to_numpy()
        dsub = primary_s["nbm_prob_subsample"].astype(float).to_numpy()
        diff = paired_brier_diff(yb, d9, d99)
        note(
            f"p99 Brier decile={brier(yb, d9):.6f} p99={brier(yb, d99):.6f} "
            f"subsample={brier(yb, dsub):.6f} 9-99={diff['mean']:.6f} "
            f"CI=[{diff['ci_lo']:+.6f},{diff['ci_hi']:+.6f}]"
        )
        note(
            f"max|decile-subsample| on primary={np.nanmax(np.abs(d9-dsub)):.6e} "
            f"median|decile-p99|={np.median(np.abs(d9-d99)):.6f}"
        )
        # If in_primary_band was string-True for all, n would equal all settled
        note(f"p99 settled all-rows n={len(br_s)} vs primary {len(primary_s)}")

    note(
        f"P1-P99 coverage={p99_days['cli_in_p1_p99'].mean():.4f} "
        f"below={p99_days['cli_below_p1'].mean():.4f} above={p99_days['cli_above_p99'].mean():.4f} "
        f"n_cli={p99_days['cli_high_f'].notna().sum()}"
    )
    note(
        f"neighbors nearest={p99_days['nearest_abs_err'].median():.6f} "
        f"best={p99_days['best_neighbor_abs_err'].median():.6f} "
        f"e={p99_days['e_abs_err'].median():.6f} "
        f"mean_cell={p99_days['mean_cell_abs_err'].median():.6f} "
        f"frac_oracle_closer={p99_days['neighbor_closer'].mean():.4f}"
    )

    # PIT vs window: CLI vs window are different products
    note(
        f"pit_days n={len(pit)} cli_in={pit['cli_in_p10_p90'].mean():.4f} "
        f"asos_full_in={pit['asos_in_p10_p90'].mean():.4f}"
    )

    # Canvas rounding checks
    note("--- canvas rounding ---")
    note(f"claimed 0.048 later-mkt gap actual {v12['mean']:.6f}")
    note(f"claimed CI [0.030,0.067] actual [{v12['ci_lo']:.6f},{v12['ci_hi']:.6f}]")
    note(f"claimed +0.008 vintage actual {vvt['mean']:.6f}")
    note(f"claimed T-24 0.020 actual {contract['mean']:.6f}")
    note(f"claimed 1.8c p99 MAE actual {p99_days['mae_decile_vs_p99'].median()*100:.4f} cents")
    note(f"claimed fee -0.27c actual {fee['taker_sell_after_fee'].median()*100:.4f} cents")
    note(f"claimed NBM-high -9.2c actual {high['median_taker_sell']*100:.4f} cents")

    print("\n=== FINDING LIST ===")
    for line in findings:
        if (
            line.startswith("FAIL")
            or "anomal" in line.lower()
            or "DAY-clustered" in line
            or "ON T-12" in line
            or "frac_out" in line
        ):
            print(">", line)


if __name__ == "__main__":
    main()
