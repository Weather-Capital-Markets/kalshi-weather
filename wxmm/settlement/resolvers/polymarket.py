"""Polymarket WU-KLGA accept-until-next-first-datapoint resolver."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from wxmm.core.utc import require_utc
from wxmm.settlement.eras import polymarket_rule_for
from wxmm.settlement.rules import Observation, SettlementResult


def resolve(
    climate_day: date,
    observations: tuple[Observation, ...] | list[Observation],
    as_of: datetime,
) -> SettlementResult:
    """Latest KLGA observation for ``climate_day`` accepted under the WU rule.

    Revisions are accepted until the next climate day's first datapoint.
    Cutoff is exclusive of that first datapoint. If that datapoint is not
    yet visible at ``as_of``, revisions remain open up to ``as_of``.
    """
    as_of_utc = require_utc(as_of)
    rule = polymarket_rule_for(climate_day)
    station = rule.underlying.station
    next_day = climate_day + timedelta(days=1)
    next_first_times = [
        obs.available_at
        for obs in observations
        if obs.station == station
        and obs.climate_day == next_day
        and obs.available_at <= as_of_utc
    ]
    revision_cutoff = min(next_first_times) if next_first_times else None

    candidates: list[Observation] = []
    for obs in observations:
        if obs.climate_day != climate_day or obs.station != station:
            continue
        if obs.available_at > as_of_utc:
            continue
        if revision_cutoff is not None and obs.available_at >= revision_cutoff:
            continue
        candidates.append(obs)

    if not candidates:
        return SettlementResult(
            climate_day=climate_day,
            venue="polymarket",
            rule_id=rule.rule_id,
            high_f=None,
            snapshot_at=revision_cutoff,
            observation_available_at=None,
            tagged_assumption=rule.tagged_assumption,
            pending=True,
            source="none",
        )
    chosen = max(candidates, key=lambda obs: (obs.available_at, obs.valid_at))
    return SettlementResult(
        climate_day=climate_day,
        venue="polymarket",
        rule_id=rule.rule_id,
        high_f=chosen.high_f,
        snapshot_at=revision_cutoff,
        observation_available_at=chosen.available_at,
        tagged_assumption=rule.tagged_assumption,
        pending=False,
        source=chosen.source,
    )
