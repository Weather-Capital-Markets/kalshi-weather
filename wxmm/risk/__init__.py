"""Risk: positions, exposure, basis distributions, hedges, limits.

Allowed to assume
    IEM ASOS station-basis numbers given for Stage B1 (2021-01-01 … 2026-08-19,
    n=2057): delta = round(max KLGA) − round(max KNYC); median +1.0°F;
    KLGA warmer 54.1% (67.6% JJA). Even-edged 2°F bracket disagreement:
    overall 23.3%, middle 39–85°F 21.2%, JJA 61.5%, SON 18.5%, MAM 11.6%,
    DJF 0.0% (DJF is a binning artifact; only JJA and the middle band are
    measurements). Kalshi NYC ≠ Polymarket NYC.

Must never
    Return a point estimate of settlement difference. Net non-fungible
    underlyings. Report residual basis as zero on a cross-underlying hedge.
    Call wall-clock. Invent disagreement rates.
"""

from __future__ import annotations
