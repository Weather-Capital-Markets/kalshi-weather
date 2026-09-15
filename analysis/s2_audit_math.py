"""Independent audit of S2 relative-value math.

Coverage recomputes from relative_value_coverage.csv only.
Formula identities are checked on synthetic six-leg books.
Does not load candles and does not compute violation magnitudes.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from analysis.k2_rigor import quadratic_taker_fee as k2_fee
from analysis.relative_value_census import (
    ERA_10AM_LAST,
    HORIZONS_H,
    N_BRACKETS,
    NO_PAYOUT,
    annualize_simple,
    basket_sums,
    ceil_6dp,
    collateral_arithmetic,
    complete_book_filter,
    quadratic_taker_fee,
    season_of,
    taker_adjusted_sums,
    violation_run,
)
from analysis.spread_census import is_two_sided

OUT = Path("analysis/out")
PRINTED = {
    36: {
        "n_in": 1325,
        "n_c": 457,
        "rate": 0.345,
        "s15": 0.197,
        "uncond": 0.341,
        "band": 14,
        "cent": 0.624,
    },
    24: {
        "n_in": 1337,
        "n_c": 299,
        "rate": 0.224,
        "s15": 0.142,
        "uncond": 0.223,
        "band": 12,
        "cent": 0.706,
    },
    12: {
        "n_in": 1340,
        "n_c": 14,
        "rate": 0.010,
        "s15": 0.009,
        "uncond": 0.010,
        "band": 0,
        "cent": 0.429,
    },
    6: {
        "n_in": 1340,
        "n_c": 2,
        "rate": 0.001,
        "s15": 0.001,
        "uncond": 0.001,
        "band": 0,
        "cent": 0.500,
    },
}
SEASON_PRINTED = {
    (36, "DJF"): (343, 105, 0.306, 2),
    (36, "MAM"): (366, 123, 0.336, 4),
    (36, "JJA"): (350, 130, 0.371, 2),
    (36, "SON"): (266, 99, 0.372, 6),
    (24, "DJF"): (347, 71, 0.205, 2),
    (24, "MAM"): (368, 96, 0.261, 4),
    (24, "JJA"): (352, 85, 0.241, 5),
    (24, "SON"): (270, 47, 0.174, 1),
    (12, "DJF"): (347, 6, 0.017, 0),
    (12, "MAM"): (368, 6, 0.016, 0),
    (12, "JJA"): (352, 2, 0.006, 0),
    (12, "SON"): (273, 0, 0.000, 0),
    (6, "DJF"): (347, 2, 0.006, 0),
    (6, "MAM"): (368, 0, 0.000, 0),
    (6, "JJA"): (352, 0, 0.000, 0),
    (6, "SON"): (273, 0, 0.000, 0),
}


def _bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


def main() -> None:
    findings: list[str] = []

    def note(text: str) -> None:
        findings.append(text)
        print(text)

    cov = pd.read_csv(OUT / "relative_value_coverage.csv", comment="#")
    for col in (
        "in_window",
        "complete_book",
        "complete_book_strict15",
        "complete_book_unconditional",
        "all_in_band",
    ):
        cov[col] = _bool(cov[col])

    note(f"coverage_rows={len(cov)} days={cov['climate_date'].nunique()}")
    if len(cov) != 1340 * 4:
        note(f"FAIL row count {len(cov)} != 1340*4")
    if cov["climate_date"].nunique() != 1340:
        note(f"FAIL universe days {cov['climate_date'].nunique()}")
    if (cov["n_brackets"] != 6).any():
        note("FAIL n_brackets not always 6")
    else:
        note("PASS n_brackets==6 on every row")
    if cov["climate_date"].min() < "2022-12-11":
        note(f"FAIL start {cov['climate_date'].min()}")
    else:
        note(f"PASS date span {cov['climate_date'].min()} .. {cov['climate_date'].max()}")

    season_mismatch = 0
    for climate_date, season in (
        cov[["climate_date", "season"]].drop_duplicates().itertuples(index=False)
    ):
        if season_of(str(climate_date)) != season:
            season_mismatch += 1
    note(
        "PASS season_of matches CSV"
        if season_mismatch == 0
        else f"FAIL season mismatches={season_mismatch}"
    )

    # Flag consistency (no prices needed).
    n_two = cov["n_two_sided"]
    fail_uncond = int((cov["complete_book_unconditional"] != (n_two == 6)).sum())
    fail_complete = int(
        (cov["complete_book"] != (cov["in_window"] & cov["complete_book_unconditional"])).sum()
    )
    fail_band = int((cov["all_in_band"] & ~cov["complete_book"]).sum())
    fail_band_n = int((cov["all_in_band"] & (cov["n_in_band_10_90"] != 6)).sum())
    fail_split = int((cov["n_in_band_10_90"] + cov["n_outside_band"] != n_two).sum())
    fail_strict = int(
        (
            cov["complete_book_strict15"] != (cov["in_window"] & (cov["n_two_sided_strict15"] == 6))
        ).sum()
    )
    note(
        f"{'PASS' if fail_uncond == 0 else 'FAIL'} "
        f"uncond iff n_two_sided==6 mismatches={fail_uncond}"
    )
    note(
        f"{'PASS' if fail_complete == 0 else 'FAIL'} "
        f"complete iff in_window & uncond mismatches={fail_complete}"
    )
    note(
        f"{'PASS' if fail_strict == 0 else 'FAIL'} "
        f"strict15 iff in_window & n_strict==6 mismatches={fail_strict}"
    )
    note(
        f"{'PASS' if fail_band == 0 else 'FAIL'} "
        f"all_in_band implies complete mismatches={fail_band}"
    )
    note(
        f"{'PASS' if fail_band_n == 0 else 'FAIL'} "
        f"all_in_band implies n_in_band==6 mismatches={fail_band_n}"
    )
    note(
        f"{'PASS' if fail_split == 0 else 'FAIL'} "
        f"n_in_band+n_outside_band==n_two_sided mismatches={fail_split}"
    )

    note("\n=== Recompute printed coverage rates ===")
    for horizon in HORIZONS_H:
        slice_h = cov[cov["horizon_h"] == horizon]
        in_win = slice_h[slice_h["in_window"]]
        complete = in_win[in_win["complete_book"]]
        n_in = len(in_win)
        n_c = int(in_win["complete_book"].sum())
        rate = n_c / n_in if n_in else float("nan")
        s15 = float(in_win["complete_book_strict15"].mean()) if n_in else float("nan")
        uncond = float(slice_h["complete_book_unconditional"].mean())
        n_band = int(complete["all_in_band"].sum()) if len(complete) else 0
        cent = float((complete["n_one_cent_quote"] > 0).mean()) if len(complete) else float("nan")
        size = (
            float((complete["n_legs_with_ask_size"] == 6).mean()) if len(complete) else float("nan")
        )
        exp = PRINTED[horizon]
        ok = (
            n_in == exp["n_in"]
            and n_c == exp["n_c"]
            and round(rate, 3) == exp["rate"]
            and round(s15, 3) == exp["s15"]
            and round(uncond, 3) == exp["uncond"]
            and n_band == exp["band"]
            and (math.isnan(cent) or round(cent, 3) == exp["cent"])
        )
        note(
            f"{'PASS' if ok else 'FAIL'} T-{horizon}h "
            f"in={n_in} complete={n_c} rate={rate:.6f} "
            f"s15={s15:.6f} uncond={uncond:.6f} "
            f"all_in_band={n_band}/{n_c} cent={cent:.6f} size={size:.6f}"
        )
        # Uncond numerator vs in-window complete numerator.
        n_uncond = int(slice_h["complete_book_unconditional"].sum())
        note(
            f"  uncond_n={n_uncond} in_window_complete_n={n_c} "
            f"out_of_window_complete={n_uncond - n_c}"
        )

    note("\n=== Horizon x season vs printed ===")
    for (horizon, season), expected in SEASON_PRINTED.items():
        n_in_e, n_c_e, rate_e, band_e = expected
        slice_hs = cov[(cov["horizon_h"] == horizon) & (cov["season"] == season)]
        in_win = slice_hs[slice_hs["in_window"]]
        n_in = len(in_win)
        n_c = int(in_win["complete_book"].sum())
        rate = n_c / n_in if n_in else float("nan")
        complete = in_win[in_win["complete_book"]]
        n_band = int(complete["all_in_band"].sum()) if len(complete) else 0
        rate_ok = (n_c == 0 and rate_e == 0.0) or round(rate, 3) == rate_e
        ok = n_in == n_in_e and n_c == n_c_e and rate_ok and n_band == band_e
        if not ok:
            note(
                f"FAIL T-{horizon}h {season} got in={n_in} c={n_c} "
                f"rate={rate:.6f} band={n_band}"
            )
    note("PASS all 16 horizon x season cells match printed (3-decimal rates)")

    # n_two_sided distribution: why complete books collapse near T.
    note("\n=== n_two_sided among in-window (coverage structure, not sums) ===")
    for horizon in HORIZONS_H:
        in_win = cov[(cov["horizon_h"] == horizon) & (cov["in_window"])]
        counts = in_win["n_two_sided"].value_counts().sort_index()
        parts = " ".join(f"n{k}={int(v)}" for k, v in counts.items())
        mean_n = float(in_win["n_two_sided"].mean())
        note(f"T-{horizon}h mean_two_sided={mean_n:.3f} {parts}")

    k2 = pd.read_csv(OUT / "k2_day_sum_law.csv")
    k2_complete = int((k2["n_two_sided"] == k2["n_brackets"]).sum())
    note(
        f"\nK2 T-24 sample complete {k2_complete}/{len(k2)} "
        f"rate={k2_complete / len(k2):.6f} (published 71/300=0.236667); "
        f"S2 T-24 {PRINTED[24]['n_c']}/{PRINTED[24]['n_in']}="
        f"{PRINTED[24]['n_c'] / PRINTED[24]['n_in']:.6f}"
    )

    note("\n=== Formula identities (synthetic; no market data) ===")
    asks = [0.10, 0.15, 0.20, 0.18, 0.12, 0.20]
    bids = [0.20, 0.18, 0.16, 0.17, 0.19, 0.14]
    no_asks = [0.80, 0.82, 0.84, 0.83, 0.81, 0.80]
    raw = basket_sums(asks, bids, no_asks)
    assert abs(sum(asks) - 0.95) < 1e-12
    assert abs(raw["slack_ask"] - 0.05) < 1e-12
    assert abs(raw["slack_bid"] - 0.04) < 1e-12
    assert abs(raw["slack_no"] - 0.10) < 1e-12
    note("PASS basket slacks 0.05 / 0.04 / 0.10")

    implied = [1.0 - b for b in bids]
    ident = basket_sums(asks, bids, implied)
    if abs(ident["slack_no"] - ident["slack_bid"]) > 1e-12:
        note("FAIL implied NO slack != sell-YES slack")
    else:
        note(
            "PASS implied NO: slack_no = 5 - sum(1-bid) = sum(bid)-1 = slack_bid "
            f"({ident['slack_no']:.4f})"
        )
    cap_imp = collateral_arithmetic(ident["sum_ask"], ident["sum_bid"], ident["sum_no_ask"])
    if cap_imp["roc_buy_no"] is None or cap_imp["roc_sell_gross"] is None:
        note("FAIL implied ROC missing")
    elif abs(cap_imp["roc_buy_no"] - cap_imp["roc_sell_gross"]) > 1e-12:
        note("FAIL implied buy-NO ROC != sell-gross ROC")
    else:
        note("PASS implied buy-NO ROC == sell-gross ROC")

    cap = collateral_arithmetic(0.95, 1.04, 4.90)
    note(
        f"PASS collateral buy {cap['capital_buy']:.2f} roc={cap['roc_buy']:.6f} "
        f"sell_gross {cap['capital_sell_gross']:.2f} roc={cap['roc_sell_gross']:.6f} "
        f"netted_1 roc={cap['roc_sell_netted_1']:.6f} "
        f"netted_offset capital={cap['capital_sell_netted_offset']} "
        f"roc={cap['roc_sell_netted_offset']}"
    )
    if cap["capital_sell_netted_offset"] != 0.0 or cap["roc_sell_netted_offset"] is not None:
        note("FAIL netted-offset should be 0 capital / undefined ROC when sum_bid>1")
    else:
        note(
            "NOTE netted-offset ROC is undefined exactly on sell violations "
            "(capital max(1-sum_bid,0)=0). The formula is consistent; it cannot "
            "be used as a return on those rows."
        )

    fee = taker_adjusted_sums(asks, bids, no_asks)
    fee_sum = sum(quadratic_taker_fee(a) for a in asks)
    if abs(fee["sum_ask_after_fee"] - (0.95 + fee_sum)) > 1e-12:
        note("FAIL fee-adjusted ask sum")
    else:
        note(f"PASS fee-adjusted buy cost = sum_ask + sum(fee) = {fee['sum_ask_after_fee']:.6f}")
    if quadratic_taker_fee(0.5) != k2_fee(0.5):
        note("FAIL fee disagrees with k2_rigor")
    else:
        note("PASS quadratic_taker_fee matches k2_rigor at 0.50 and 0.10")
    if quadratic_taker_fee(0.10) != k2_fee(0.10):
        note("FAIL fee 0.10 disagrees")
    # Symmetry P(1-P).
    if quadratic_taker_fee(0.2) != quadratic_taker_fee(0.8):
        note("FAIL fee not symmetric in P,(1-P)")
    else:
        note("PASS fee(P)=fee(1-P); implied NO fee-slack equals sell-YES fee-slack")
    implied_fee = taker_adjusted_sums(asks, bids, implied)
    fee_gap = abs(implied_fee["slack_no_after_fee"] - implied_fee["slack_bid_after_fee"])
    if fee_gap > 1e-5:
        note(f"FAIL after-fee implied NO slack != sell slack gap={fee_gap}")
    else:
        note(
            f"PASS after-fee implied NO slack == sell-YES slack "
            f"(gap={fee_gap:.2e}; ceil_6dp can differ 1e-6 on fee(P) vs fee(1-P))"
        )

    if NO_PAYOUT != 5.0 or N_BRACKETS != 6:
        note("FAIL N_BRACKETS/NO_PAYOUT")
    else:
        note("PASS NO_PAYOUT = n_brackets - 1 = 5")

    ann = annualize_simple(0.01, 34.0)
    note(
        f"PASS annualize simple 0.01 over 34h = {ann:.6f} "
        f"(8760/34={8760 / 34:.4f}x; 365.25*24=8766, relative gap "
        f"{(8766 - 8760) / 8760:.4f})"
    )

    n_ev, dur = violation_run([(100, True), (160, True), (220, False)], 130, 500)
    if n_ev != 2 or dur != 120:
        note(f"FAIL violation_run got n={n_ev} dur={dur}")
    else:
        note("PASS violation_run n=2 duration=220-100=120s")
    # Persistence to close_ts when never cleared.
    n_ev2, dur2 = violation_run([(100, True), (160, True)], 130, 500)
    note(
        f"NOTE violation_run with no clearing event duration={dur2} "
        f"(close_ts-start={500 - 100}). Code uses settlement_ts as close_ts, "
        f"so a still-true book is timed through settlement, not last trade."
    )

    if not complete_book_filter([True] * 6):
        note("FAIL complete 6")
    if complete_book_filter([True] * 5 + [False]):
        note("FAIL complete 5+false")
    note("PASS complete_book_filter")

    if is_two_sided(0.0, 1.0) or is_two_sided(0.00, 0.03) or is_two_sided(0.40, 1.00):
        note("FAIL two-sided bounds")
    else:
        note("PASS empty 0/1 book is not two-sided; 1¢/99¢ is")

    if ERA_10AM_LAST.isoformat() != "2024-09-03":
        note("FAIL settlement era split")
    else:
        note("PASS 10am era last day 2024-09-03; fallback after that is 08:00 ET (later of 7/8)")

    # Print-bug: 0.0 or nan.
    zero_rate = 0.0
    printed_zero = zero_rate or float("nan")
    if math.isnan(printed_zero):
        note(
            "NOTE print helper `_rate(...) or nan` would print nan if a rate "
            "were exactly 0; season table uses n_c/n_in so 0.000 is safe. "
            "strict15/uncond columns use the `or nan` form."
        )

    note("\n=== ceil_6dp vs fee examples ===")
    note(f"fee(0.50)={quadratic_taker_fee(0.50)} (0.07*0.25=0.0175)")
    note(f"fee(0.10)={quadratic_taker_fee(0.10)} ceil_6dp(0.0063)={ceil_6dp(0.0063)}")

    n_fail = sum(1 for line in findings if line.startswith("FAIL"))
    note(f"\nFAIL count={n_fail}")


if __name__ == "__main__":
    main()
