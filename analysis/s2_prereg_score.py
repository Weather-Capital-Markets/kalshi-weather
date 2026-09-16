"""Score S2 v1 pre-registration from relative_value_census.csv. No new extracts."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

OUT = Path("analysis/out/relative_value_census.csv")


def _bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    return s.astype(str).str.lower().isin(["true", "1", "yes"])


def score_horizon(frame: pd.DataFrame, horizon: int, n_in_window: int) -> dict:
    h = frame[(frame["horizon_h"] == horizon) & _bool(frame["complete_book"])].copy()
    n_c = len(h)
    rate = n_c / n_in_window if n_in_window else float("nan")
    buy = _bool(h["buy_violation"])
    sell = _bool(h["sell_violation"])
    no = _bool(h["no_violation"])
    buy_n = pd.to_numeric(h["buy_run_n_candles"], errors="coerce")
    sell_n = pd.to_numeric(h["sell_run_n_candles"], errors="coerce")
    no_n = pd.to_numeric(h["no_run_n_candles"], errors="coerce")
    buy_p = buy & (buy_n >= 2)
    sell_p = sell & (sell_n >= 2)
    no_p = no & (no_n >= 2)
    union_p = buy_p | sell_p
    n_union = int(union_p.sum())
    frac = n_union / n_c if n_c else float("nan")
    days_yr = rate * frac * 365.0
    slack_ask_fee = pd.to_numeric(h["slack_ask_after_fee"], errors="coerce")
    slack_bid_fee = pd.to_numeric(h["slack_bid_after_fee"], errors="coerce")
    slack_ask = pd.to_numeric(h["slack_ask"], errors="coerce")
    slack_bid = pd.to_numeric(h["slack_bid"], errors="coerce")
    roc_buy = pd.to_numeric(h["roc_buy"], errors="coerce")
    sell_fee_still = sell_p & (slack_bid_fee > 0)
    buy_fee_still = buy_p & (slack_ask_fee > 0)
    band = h[_bool(h["all_in_band"])]
    return {
        "horizon": horizon,
        "n_complete": n_c,
        "n_in_window": n_in_window,
        "complete_rate": rate,
        "n_buy": int(buy.sum()),
        "n_sell": int(sell.sum()),
        "n_no": int(no.sum()),
        "n_buy_persist": int(buy_p.sum()),
        "n_sell_persist": int(sell_p.sum()),
        "n_no_persist": int(no_p.sum()),
        "n_union_persist": n_union,
        "frac_persist": frac,
        "days_yr": days_yr,
        "med_slack_ask_raw_buy_p": float(slack_ask[buy_p].median()) if buy_p.any() else None,
        "med_slack_bid_raw_sell_p": float(slack_bid[sell_p].median()) if sell_p.any() else None,
        "med_slack_ask_fee_buy_p": float(slack_ask_fee[buy_p].median()) if buy_p.any() else None,
        "med_slack_bid_fee_sell_p": float(slack_bid_fee[sell_p].median()) if sell_p.any() else None,
        "n_sell_persist_after_fee_still": int(sell_fee_still.sum()),
        "n_buy_persist_after_fee_still": int(buy_fee_still.sum()),
        "med_roc_buy_union_p": float(roc_buy[union_p].median()) if union_p.any() else None,
        "med_roc_buy_buy_p": float(roc_buy[buy_p].median()) if buy_p.any() else None,
        "n_band": len(band),
        "n_band_buy": int(_bool(band["buy_violation"]).sum()) if len(band) else 0,
        "n_band_sell": int(_bool(band["sell_violation"]).sum()) if len(band) else 0,
        "n_band_buy_persist": int(
            (
                _bool(band["buy_violation"])
                & (pd.to_numeric(band["buy_run_n_candles"], errors="coerce") >= 2)
            ).sum()
        )
        if len(band)
        else 0,
        "n_band_sell_persist": int(
            (
                _bool(band["sell_violation"])
                & (pd.to_numeric(band["sell_run_n_candles"], errors="coerce") >= 2)
            ).sum()
        )
        if len(band)
        else 0,
        "n_penny_excl_buy": int(_bool(h["penny_excluded_buy_violation"]).sum())
        if "penny_excluded_buy_violation" in h.columns
        else None,
        "n_penny_excl_sell": int(_bool(h["penny_excluded_sell_violation"]).sum())
        if "penny_excluded_sell_violation" in h.columns
        else None,
        "n_slack_from_penny": int(_bool(h["slack_from_penny_leg"]).sum())
        if "slack_from_penny_leg" in h.columns
        else None,
        "n_union_persist_survives_excl_penny": int(
            (union_p & ~_bool(h["slack_from_penny_leg"])).sum()
        )
        if "slack_from_penny_leg" in h.columns
        else None,
        "n_union_persist_from_penny": int((union_p & _bool(h["slack_from_penny_leg"])).sum())
        if "slack_from_penny_leg" in h.columns
        else None,
        "p50_sum_ask": float(h["sum_ask"].median()) if n_c else None,
        "p50_sum_bid": float(h["sum_bid"].median()) if n_c else None,
        "p50_sum_ask_fee": float(h["sum_ask_after_fee"].median()) if n_c else None,
        "p50_sum_bid_fee": float(h["sum_bid_after_fee"].median()) if n_c else None,
        "p50_roc_buy_all": float(roc_buy.median()) if n_c else None,
    }


def main() -> None:
    frame = pd.read_csv(OUT, comment="#")
    t24 = score_horizon(frame, 24, 1337)
    t36 = score_horizon(frame, 36, 1325)
    t12 = score_horizon(frame, 12, 1340)
    t6 = score_horizon(frame, 6, 1340)
    for row in (t24, t36, t12, t6):
        print(f"\n=== T-{row['horizon']}h ===")
        for k, v in row.items():
            if k == "horizon":
                continue
            if isinstance(v, float):
                print(f"  {k}={v:.6f}")
            else:
                print(f"  {k}={v}")


if __name__ == "__main__":
    main()
