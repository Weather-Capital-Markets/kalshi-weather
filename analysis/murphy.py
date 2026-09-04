"""Murphy Brier decomposition (reliability, resolution, uncertainty).

Bootstrap helpers live here so K2 scoring is importable without running the
diagnostics scripts.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def murphy_decompose(
    yes: np.ndarray,
    pred: np.ndarray,
    *,
    n_bins: int = 10,
) -> dict[str, float]:
    """Murphy decomposition: Brier = reliability - resolution + uncertainty."""
    yes = np.asarray(yes, dtype=float)
    pred = np.asarray(pred, dtype=float)
    mask = np.isfinite(yes) & np.isfinite(pred)
    yes = yes[mask]
    pred = pred[mask]
    n = len(yes)
    if n == 0:
        return {
            "n": 0.0,
            "brier": float("nan"),
            "reliability": float("nan"),
            "resolution": float("nan"),
            "uncertainty": float("nan"),
        }
    o_bar = float(yes.mean())
    uncertainty = o_bar * (1.0 - o_bar)
    brier = float(((pred - yes) ** 2).mean())
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    reliability = 0.0
    resolution = 0.0
    for i in range(n_bins):
        if i == 0:
            in_bin = (pred >= edges[i]) & (pred <= edges[i + 1])
        else:
            in_bin = (pred > edges[i]) & (pred <= edges[i + 1])
        count = int(in_bin.sum())
        if count == 0:
            continue
        f_k = float(pred[in_bin].mean())
        o_k = float(yes[in_bin].mean())
        weight = count / n
        reliability += weight * (f_k - o_k) ** 2
        resolution += weight * (o_k - o_bar) ** 2
    return {
        "n": float(n),
        "brier": brier,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "reconstructed": reliability - resolution + uncertainty,
    }


def bootstrap_mean_ci(
    values: np.ndarray,
    *,
    n_boot: int = 2000,
    seed: int = 43,
    alpha: float = 0.05,
) -> dict[str, float]:
    """Percentile bootstrap CI for the mean of an observation-level series."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n == 0:
        return {
            "n": 0.0,
            "mean": float("nan"),
            "ci_lo": float("nan"),
            "ci_hi": float("nan"),
        }
    rng = np.random.default_rng(seed)
    draws = np.empty(n_boot)
    for i in range(n_boot):
        draws[i] = values[rng.integers(0, n, n)].mean()
    return {
        "n": float(n),
        "mean": float(values.mean()),
        "ci_lo": float(np.quantile(draws, alpha / 2.0)),
        "ci_hi": float(np.quantile(draws, 1.0 - alpha / 2.0)),
    }


def paired_brier_diff(
    yes: np.ndarray,
    pred_a: np.ndarray,
    pred_b: np.ndarray,
    *,
    n_boot: int = 2000,
    seed: int = 43,
) -> dict[str, float]:
    """Mean of (Brier_a − Brier_b); positive means A is worse than B."""
    yes = np.asarray(yes, dtype=float)
    pred_a = np.asarray(pred_a, dtype=float)
    pred_b = np.asarray(pred_b, dtype=float)
    mask = np.isfinite(yes) & np.isfinite(pred_a) & np.isfinite(pred_b)
    se_a = (pred_a[mask] - yes[mask]) ** 2
    se_b = (pred_b[mask] - yes[mask]) ** 2
    out = bootstrap_mean_ci(se_a - se_b, n_boot=n_boot, seed=seed)
    out["brier_a"] = float(se_a.mean()) if len(se_a) else float("nan")
    out["brier_b"] = float(se_b.mean()) if len(se_b) else float("nan")
    out["frac_a_better"] = float((se_a < se_b).mean()) if len(se_a) else float("nan")
    return out


def day_clustered_brier_diff(
    frame: pd.DataFrame,
    a_col: str,
    b_col: str,
    *,
    yes_col: str = "settled_yes",
    day_col: str = "climate_date",
    n_boot: int = 2000,
    seed: int = 43,
) -> dict[str, float]:
    """Bootstrap days, not contracts. Each day contributes its mean SE difference."""
    rows: list[float] = []
    for _, group in frame.groupby(day_col):
        yes = group[yes_col].astype(float).to_numpy()
        pred_a = group[a_col].astype(float).to_numpy()
        pred_b = group[b_col].astype(float).to_numpy()
        mask = np.isfinite(yes) & np.isfinite(pred_a) & np.isfinite(pred_b)
        if not mask.any():
            continue
        rows.append(
            float((((pred_a[mask] - yes[mask]) ** 2) - ((pred_b[mask] - yes[mask]) ** 2)).mean())
        )
    return bootstrap_mean_ci(np.asarray(rows, dtype=float), n_boot=n_boot, seed=seed)


def murphy_model_rows(
    settled: pd.DataFrame,
    models: list[tuple[str, str]],
    *,
    yes_col: str = "settled_yes",
) -> list[dict[str, Any]]:
    """One Murphy row per (model_name, probability_column)."""
    rows: list[dict[str, Any]] = []
    yes_all = settled[yes_col].astype(float)
    for name, col in models:
        if col not in settled.columns:
            continue
        mask = yes_all.notna() & settled[col].notna()
        if not mask.any():
            continue
        out = murphy_decompose(yes_all[mask].to_numpy(), settled.loc[mask, col].to_numpy())
        rows.append({"model": name, **out})
    return rows
