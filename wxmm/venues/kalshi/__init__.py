"""Kalshi adapter package.

Allowed to assume
    Facts in ``knowledge/venue-facts.md`` for KXHIGHNY / HIGHNY. Maker fee
    $0.00 verified 2026-08-15 with re-verify-before-capital. Taker formula
    ``round_up(0.07 · C · P · (1−P))`` as given for Stage B1; rounding
    granularity is a model parameter.

Must never
    Hard-code bracket widths (boundary convention UNVERIFIED). Assume
    emit-on-change is proven. Download whole NBM files. Call wall-clock.
"""

from __future__ import annotations
