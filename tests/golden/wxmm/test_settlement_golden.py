"""Settlement golden files: one per Kalshi era plus Polymarket, plus a revision split."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from wxmm.settlement.resolvers import kalshi as kalshi_resolver
from wxmm.settlement.resolvers import polymarket as pm_resolver
from wxmm.settlement.rules import Observation

GOLDEN = Path(__file__).resolve().parent


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _obs(row: dict[str, object]) -> Observation:
    high = row["high_f"]
    return Observation(
        station=str(row["station"]),
        climate_day=date.fromisoformat(str(row["climate_day"])),
        high_f=int(high) if high is not None else None,
        valid_at=_parse_ts(str(row["valid_at"])),
        available_at=_parse_ts(str(row["available_at"])),
        source=str(row["source"]),
        is_full_day=bool(row["is_full_day"]),
        is_revision=bool(row["is_revision"]),
    )


def _run_venue(venue: str, climate: date, observations: list[Observation], as_of: datetime):
    if venue == "kalshi":
        return kalshi_resolver.resolve(climate, observations, as_of)
    if venue == "polymarket":
        return pm_resolver.resolve(climate, observations, as_of)
    raise ValueError(venue)


def test_kalshi_unspecified_era_golden() -> None:
    _assert_single(GOLDEN / "settlement_kalshi_unspecified.json")


def test_kalshi_10am_era_golden() -> None:
    _assert_single(GOLDEN / "settlement_kalshi_10am.json")


def test_kalshi_7or8am_era_golden() -> None:
    _assert_single(GOLDEN / "settlement_kalshi_7or8am.json")


def test_polymarket_golden() -> None:
    _assert_single(GOLDEN / "settlement_polymarket.json")


def test_revision_case_venues_resolve_differently() -> None:
    payload = json.loads((GOLDEN / "settlement_revision_cross_venue.json").read_text())
    climate = date.fromisoformat(payload["climate_day"])
    as_of = _parse_ts(payload["as_of"])
    kalshi_obs = [_obs(row) for row in payload["kalshi_observations"]]
    pm_obs = [_obs(row) for row in payload["polymarket_observations"]]
    kalshi = kalshi_resolver.resolve(climate, kalshi_obs, as_of)
    pm = pm_resolver.resolve(climate, pm_obs, as_of)
    assert kalshi.high_f == payload["expected_kalshi_high_f"]
    assert pm.high_f == payload["expected_polymarket_high_f"]
    assert kalshi.high_f != pm.high_f


def _assert_single(path: Path) -> None:
    payload = json.loads(path.read_text())
    climate = date.fromisoformat(payload["climate_day"])
    as_of = _parse_ts(payload["as_of"])
    observations = [_obs(row) for row in payload["observations"]]
    result = _run_venue(payload["venue"], climate, observations, as_of)
    expected = payload["expected"]
    assert result.high_f == expected["high_f"]
    assert result.pending is expected["pending"]
    assert result.rule_id == expected["rule_id"]
    needle = expected["tagged_assumption_contains"]
    if needle is None:
        assert result.tagged_assumption is None or needle not in (result.tagged_assumption or "")
        if expected.get("rule_id", "").startswith("kalshi_unspecified"):
            assert result.tagged_assumption is not None
        elif payload["venue"] == "polymarket":
            assert result.tagged_assumption is None
    else:
        assert result.tagged_assumption is not None
        assert needle in result.tagged_assumption
