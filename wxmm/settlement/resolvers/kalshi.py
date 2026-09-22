"""Kalshi CLINYC ignore-after-snapshot resolver."""

from __future__ import annotations

from datetime import date, datetime

from wxmm.core.utc import require_utc
from wxmm.settlement.eras import kalshi_rule_for, kalshi_snapshot_utc
from wxmm.settlement.rules import Observation, RevisionNotice, SettlementResult


def revision_notice(
    climate_day: date,
    observations: tuple[Observation, ...] | list[Observation],
    *,
    snapshot: datetime,
    as_of: datetime,
    station: str,
    resolved_high: int | None,
) -> RevisionNotice:
    """Issuances after the snapshot and at or before ``as_of``.

    The resolved high is not updated. ``high_changed`` is true when any
    later full-day high differs from that snapshot value.
    """
    as_of_utc = require_utc(as_of)
    snapshot_utc = require_utc(snapshot)
    later = [
        obs
        for obs in observations
        if obs.climate_day == climate_day
        and obs.station == station
        and obs.is_full_day
        and snapshot_utc < obs.available_at <= as_of_utc
    ]
    highs = tuple(obs.high_f for obs in later)
    changed = resolved_high is not None and any(high != resolved_high for high in highs)
    return RevisionNotice(n_later=len(later), later_highs=highs, high_changed=changed)


def resolve(
    climate_day: date,
    observations: tuple[Observation, ...] | list[Observation],
    as_of: datetime,
) -> SettlementResult:
    """Latest full-day KNYC observation visible at min(as_of, snapshot).

    Later issuances are detected and attached as ``revision``. They do not
    change ``high_f``.
    """
    as_of_utc = require_utc(as_of)
    rule = kalshi_rule_for(climate_day)
    snapshot = kalshi_snapshot_utc(climate_day, rule, observations)
    cutoff = min(as_of_utc, snapshot)
    station = rule.underlying.station
    candidates = [
        obs
        for obs in observations
        if obs.climate_day == climate_day
        and obs.station == station
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
            revision=revision_notice(
                climate_day,
                observations,
                snapshot=snapshot,
                as_of=as_of_utc,
                station=station,
                resolved_high=None,
            ),
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
        revision=revision_notice(
            climate_day,
            observations,
            snapshot=snapshot,
            as_of=as_of_utc,
            station=station,
            resolved_high=chosen.high_f,
        ),
    )
