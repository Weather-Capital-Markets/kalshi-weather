"""Venue Protocol and adapter factory.

Allowed to assume
    Two venues: Kalshi (KNYC/CLINYC) and Polymarket (KLGA/WU). A registry may
    accept one additional venue later without inventing its facts.

Must never
    Be imported as a concrete adapter by layers above this package
    (``from wxmm.venues.kalshi import ...`` is forbidden outside ``venues/``).
    Call ``datetime.now``. Do date arithmetic — use ``timeauth``. Price a
    Polymarket fill while the fee schedule is unverified.
"""

from __future__ import annotations

from wxmm.venues.base import Venue, get_venue

__all__ = ["Venue", "get_venue"]
