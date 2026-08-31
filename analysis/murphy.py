"""Murphy Brier decomposition (reliability, resolution, uncertainty)."""

from __future__ import annotations

import numpy as np


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
