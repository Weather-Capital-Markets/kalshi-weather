"""Kalshi and Polymarket settlement-era tables.

Dates from ``knowledge/venue-facts.md`` §1.10 (measured 2026-08-14). Do not
re-derive. The unspecified Kalshi era is a tagged assumption, not verified.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from wxmm.core.timeauth import localize_civil
from wxmm.core.underlying import KALSHI_NYC_DAILY_HIGH, POLYMARKET_NYC_DAILY_HIGH
from wxmm.core.utc import require_utc
from wxmm.settlement.rules import Observation, SettlementRule

# Snapshot policies
SNAPSHOT_FIRST_10AM_ET = "first_10am_et"
SNAPSHOT_FIRST_7_OR_8AM_ET = "first_7_or_8am_et"
SNAPSHOT_NONE_WU = "wunderground_revision_window"

REVISION_IGNORE_AFTER_SNAPSHOT = "ignore_after_snapshot"
REVISION_ACCEPT_UNTIL_NEXT_FIRST = "accept_until_next_first_datapoint"

# (verify) 7 vs 8 AM: contract says "first 7:00 or 8:00 AM ET following the
# release of the data". Implementation: 07:00 ET if a full-day report is
# already available by then, else 08:00 ET. Not a ratified intra-hour fact.
SEVEN_OR_EIGHT_INTERPRETATION = (
    "first_7am_if_full_day_report_already_available_else_8am (verify)"
)

KALSHI_UNSPECIFIED_ASSUMPTION = (
    "V1_rule_100_19_unspecified_defaults_to_first_10am_et (tagged assumption)"
)

KALSHI_RULES: tuple[SettlementRule, ...] = (
    SettlementRule(
        venue="kalshi",
        underlying=KALSHI_NYC_DAILY_HIGH,
        rule_id="kalshi_unspecified_thru_2021-12-25",
        effective_from=date(2021, 8, 6),
        effective_to=date(2021, 12, 25),
        snapshot_policy=SNAPSHOT_FIRST_10AM_ET,
        revision_policy=REVISION_IGNORE_AFTER_SNAPSHOT,
        tagged_assumption=KALSHI_UNSPECIFIED_ASSUMPTION,
    ),
    SettlementRule(
        venue="kalshi",
        underlying=KALSHI_NYC_DAILY_HIGH,
        rule_id="kalshi_first_10am_et_thru_2024-09-03",
        effective_from=date(2021, 12, 26),
        effective_to=date(2024, 9, 3),
        snapshot_policy=SNAPSHOT_FIRST_10AM_ET,
        revision_policy=REVISION_IGNORE_AFTER_SNAPSHOT,
        tagged_assumption=None,
    ),
    SettlementRule(
        venue="kalshi",
        underlying=KALSHI_NYC_DAILY_HIGH,
        rule_id="kalshi_first_7_or_8am_et_from_2024-09-04",
        effective_from=date(2024, 9, 4),
        effective_to=None,
        snapshot_policy=SNAPSHOT_FIRST_7_OR_8AM_ET,
        revision_policy=REVISION_IGNORE_AFTER_SNAPSHOT,
        tagged_assumption=SEVEN_OR_EIGHT_INTERPRETATION,
    ),
)

POLYMARKET_RULES: tuple[SettlementRule, ...] = (
    SettlementRule(
        venue="polymarket",
        underlying=POLYMARKET_NYC_DAILY_HIGH,
        rule_id="polymarket_wu_klga_accept_until_next_first",
        effective_from=date(2021, 1, 1),
        effective_to=None,
        snapshot_policy=SNAPSHOT_NONE_WU,
        revision_policy=REVISION_ACCEPT_UNTIL_NEXT_FIRST,
        tagged_assumption=None,
    ),
)


def kalshi_rule_for(climate_day: date) -> SettlementRule:
    for rule in KALSHI_RULES:
        if rule.covers(climate_day):
            return rule
    raise KeyError(f"no Kalshi settlement rule for climate_day={climate_day.isoformat()}")


def polymarket_rule_for(climate_day: date) -> SettlementRule:
    for rule in POLYMARKET_RULES:
        if rule.covers(climate_day):
            return rule
    raise KeyError(f"no Polymarket settlement rule for climate_day={climate_day.isoformat()}")


def rule_for(venue: str, climate_day: date) -> SettlementRule:
    if venue == "kalshi":
        return kalshi_rule_for(climate_day)
    if venue == "polymarket":
        return polymarket_rule_for(climate_day)
    raise KeyError(f"unknown venue {venue!r}")


def kalshi_snapshot_utc(
    climate_day: date,
    rule: SettlementRule,
    observations: tuple[Observation, ...] | list[Observation],
) -> datetime:
    """Settlement snapshot instant in UTC. Climate day D snaps on D+1 civil ET."""
    plus_one = climate_day + timedelta(days=1)
    station = rule.underlying.station
    if rule.snapshot_policy == SNAPSHOT_FIRST_10AM_ET:
        return localize_civil(station, plus_one, hour=10, minute=0)
    if rule.snapshot_policy == SNAPSHOT_FIRST_7_OR_8AM_ET:
        seven = localize_civil(station, plus_one, hour=7, minute=0)
        eight = localize_civil(station, plus_one, hour=8, minute=0)
        for obs in observations:
            if (
                obs.climate_day == climate_day
                and obs.station == station
                and obs.is_full_day
                and obs.available_at <= seven
            ):
                return seven
        return eight
    raise ValueError(f"unsupported Kalshi snapshot_policy {rule.snapshot_policy!r}")


def settlement_rule_in_force(venue: str, as_of: datetime) -> SettlementRule:
    """Era lookup by the climate day of ``as_of`` at the venue's station."""
    from wxmm.core.timeauth import climate_day as climate_day_of

    as_of_utc = require_utc(as_of)
    if venue == "kalshi":
        day = climate_day_of(as_of_utc, "KNYC")
        return kalshi_rule_for(day)
    if venue == "polymarket":
        day = climate_day_of(as_of_utc, "KLGA")
        return polymarket_rule_for(day)
    raise KeyError(f"unknown venue {venue!r}")
