"""Polymarket adapter package.

Allowed to assume
    NYC daily high: exhaustive disjoint 2°F bins + tails (≤75, ≥94), 11
    contracts, negRisk mutually exclusive, Σp=1. Settles on KLGA via Weather
    Underground Daily Observations. 20 req/s per IP.
    Observed CLOB fee fields: rate 0.05, rebateRate 0.25, takerOnly — base
    and units UNVERIFIED.

Must never
    Price a fill (raise ``UnverifiedFeeSchedule``). Assume Kalshi and
    Polymarket NYC are fungible. Net collateral under negRisk (UNVERIFIED;
    ``CollateralPolicy`` defaults to no_netting). Call wall-clock.
"""

from __future__ import annotations
