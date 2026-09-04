"""Kalshi CLINYC ignore-after-snapshot resolver."""

from __future__ import annotations

from datetime import date, datetime

from wxmm.core.utc import require_utc
from wxmm.settlement.eras import kalshi_rule_for, kalshi_snapshot_utc
from wxmm.settlement.rules import Observation, SettlementResult


def resolve(
    climate_day: date,
    observations: tuple[Observation, ...] | list[Observation],
    as_of: datetime,
) -> SettlementResult:
    """Latest full-day KNYC observation visible at min(as_of, snapshot)."""
    as_of_utc = require_utc(as_of)
    rule = kalshi_rule_for(climate_day)
    snapshot = kalshi_snapshot_utc(climate_day, rule, observations)
    cutoff = min(as_of_utc, snapshot)
    candidates = [
        obs
        for obs in observations
        if obs.climate_day == climate_day
        and obs.station == rule.underlying.station
        and obs.is_full_day
        and obs.available_at <= cutoff
    ]
    if not candidates:
        return SettlementResult(
            climate_day=climate_day,
            venue="kalshi",
            rule_id=rule.rule_id,
            high_f=None,
            snapshot_at=snapshot,
            observation_available_at=None,
            tagged_assumption=rule.tagged_assumption,
            pending=True,
            source="none",
        )
    chosen = max(candidates, key=lambda obs: (obs.available_at, obs.valid_at))
    return SettlementResult(
        climate_day=climate_day,
        venue="kalshi",
        rule_id=rule.rule_id,
        high_f=chosen.high_f,
        snapshot_at=snapshot,
        observation_available_at=chosen.available_at,
        tagged_assumption=rule.tagged_assumption,
        pending=False,
        source=chosen.source,
    )
