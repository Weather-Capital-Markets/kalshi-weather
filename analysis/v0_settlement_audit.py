"""Phase B3 settlement-era audit and EDT midnight-hour diagnostics for v0 RUN.

Reads frozen CLINYC, settlement labels, and market metadata only. No network.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from analysis.build_labels import high_at_snapshot, read_clinyc, read_labels
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START
from wxmm.core.timeauth import climate_day, is_dst
from wxmm.settlement.brackets import strike_of, yes_won
from wxmm.settlement.eras import KALSHI_RULES, kalshi_snapshot_utc
from wxmm.settlement.rules import Observation, SettlementRule

STATION = "KNYC"
ERA_BOUNDARY_DAYS: tuple[tuple[date, date, str], ...] = (
    (date(2021, 12, 25), date(2021, 12, 26), "unspecified_to_10am"),
    (date(2024, 9, 3), date(2024, 9, 4), "10am_to_7or8am"),
)
NEAR_BOUNDARY_WINDOW_DAYS = 3
RECORDED_LABEL_NOISE_RATE = 1 / 2416


def _load_markets(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list of markets")
    return [row for row in payload if isinstance(row, dict)]


def _high_at_snapshot_for_rule(
    observations: Sequence[Observation],
    climate_day_value: date,
    rule: SettlementRule,
) -> tuple[int | None, int]:
    same_day = [o for o in observations if o.climate_day == climate_day_value]
    if not same_day:
        return None, 0
    snapshot = kalshi_snapshot_utc(climate_day_value, rule, tuple(same_day))
    visible = [o for o in same_day if o.is_full_day and o.available_at <= snapshot]
    later = [o for o in same_day if o.is_full_day and o.available_at > snapshot]
    if not visible:
        return None, len(later)
    chosen = max(visible, key=lambda o: o.available_at)
    return chosen.high_f, len(later)


def _rule_before(climate_day_value: date) -> SettlementRule | None:
    current_idx = next(
        (idx for idx, rule in enumerate(KALSHI_RULES) if rule.covers(climate_day_value)),
        None,
    )
    if current_idx is None or current_idx == 0:
        return None
    return KALSHI_RULES[current_idx - 1]


def _rule_after(climate_day_value: date) -> SettlementRule | None:
    current_idx = next(
        (idx for idx, rule in enumerate(KALSHI_RULES) if rule.covers(climate_day_value)),
        None,
    )
    if current_idx is None or current_idx >= len(KALSHI_RULES) - 1:
        return None
    return KALSHI_RULES[current_idx + 1]


def _near_era_boundary(climate_day_value: date) -> str | None:
    for before, after, label in ERA_BOUNDARY_DAYS:
        window = NEAR_BOUNDARY_WINDOW_DAYS
        for anchor in (before, after):
            if abs((climate_day_value - anchor).days) <= window:
                return label
    return None


def settlement_era_audit(
    labels_path: Path | str,
    clinyc_path: Path | str,
    markets_path: Path | str,
) -> dict[str, Any]:
    """Audit settlement labels against CLINYC snapshot rules and bracket structure."""
    labels = read_labels(Path(labels_path))
    observations = read_clinyc(Path(clinyc_path))
    markets = _load_markets(Path(markets_path))

    labels_by_day: dict[date, list[Any]] = defaultdict(list)
    for label in labels.values():
        labels_by_day[label.climate_day].append(label)

    market_by_ticker = {str(m.get("ticker") or ""): m for m in markets if m.get("ticker")}

    days_resolved_cleanly = 0
    days_ambiguous = 0
    days_raising = 0
    ambiguous_days: list[dict[str, Any]] = []
    raising_days: list[dict[str, Any]] = []
    era_boundary_rows: list[dict[str, Any]] = []

    climate_days = sorted(labels_by_day)
    for climate_day_value in climate_days:
        day_labels = labels_by_day[climate_day_value]
        winners = [label for label in day_labels if label.yes_won]
        n_winners = len(winners)

        high, n_later = high_at_snapshot(observations, climate_day_value)
        flags: list[str] = []
        if high is None:
            flags.append("missing_high_at_snapshot")
        if n_later > 0:
            flags.append(f"n_later={n_later}")
        if n_winners == 0:
            flags.append("zero_winners")
        elif n_winners > 1:
            flags.append(f"multiple_winners={n_winners}")

        for label in day_labels:
            market = market_by_ticker.get(label.ticker)
            if market is None:
                flags.append(f"missing_market:{label.ticker}")
                continue
            strike = strike_of(market)
            if strike is None:
                flags.append(f"unparsed_strike:{label.ticker}")
                continue
            if high is not None and yes_won(strike, high) != label.yes_won:
                flags.append(f"label_mismatch:{label.ticker}")
            venue = str(market.get("result") or "").strip().lower()
            if venue in {"yes", "no"} and (venue == "yes") != label.yes_won:
                flags.append(f"venue_disagree:{label.ticker}")

        if n_winners == 1 and not flags:
            days_resolved_cleanly += 1
        if n_winners == 0 or n_winners > 1:
            days_ambiguous += 1
            if len(ambiguous_days) < 50:
                ambiguous_days.append(
                    {
                        "climate_day": climate_day_value.isoformat(),
                        "n_winners": n_winners,
                        "n_brackets_labelled": len(day_labels),
                        "high_at_snapshot": high,
                        "flags": sorted(set(flags)),
                    }
                )
        if flags:
            days_raising += 1
            if len(raising_days) < 50:
                raising_days.append(
                    {
                        "climate_day": climate_day_value.isoformat(),
                        "n_winners": n_winners,
                        "high_at_snapshot": high,
                        "n_later": n_later,
                        "flags": sorted(set(flags)),
                    }
                )

        boundary_label = _near_era_boundary(climate_day_value)
        if boundary_label is None:
            continue
        current_rule = next(r for r in KALSHI_RULES if r.covers(climate_day_value))
        comparisons: list[dict[str, Any]] = []
        for alt_rule, alt_name in (
            (_rule_before(climate_day_value), "previous_rule"),
            (_rule_after(climate_day_value), "next_rule"),
        ):
            if alt_rule is None:
                continue
            alt_high, alt_later = _high_at_snapshot_for_rule(
                observations, climate_day_value, alt_rule
            )
            comparisons.append(
                {
                    "comparison": alt_name,
                    "rule_id": alt_rule.rule_id,
                    "high_at_snapshot": alt_high,
                    "n_later": alt_later,
                    "differs_from_current": alt_high != high,
                }
            )
        if comparisons:
            era_boundary_rows.append(
                {
                    "climate_day": climate_day_value.isoformat(),
                    "boundary": boundary_label,
                    "current_rule_id": current_rule.rule_id,
                    "current_high_at_snapshot": high,
                    "current_n_later": n_later,
                    "comparisons": comparisons,
                    "answer_changes": any(row["differs_from_current"] for row in comparisons),
                }
            )

    return {
        "n_climate_days_in_labels": len(climate_days),
        "days_resolved_cleanly": days_resolved_cleanly,
        "days_ambiguous": days_ambiguous,
        "days_raising": days_raising,
        "ambiguous_examples": ambiguous_days,
        "raising_examples": raising_days,
        "days_era_boundary_changes_answer": sum(
            1 for row in era_boundary_rows if row["answer_changes"]
        ),
        "era_boundary_near_days": era_boundary_rows,
        "scope_note": (
            "Labels are the CLINYC-as-issued settlement.json set; "
            "resolved_cleanly requires exactly one YES winner and no audit flags."
        ),
    }


def _asos_knyc_paths(data_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in (
        "asos/KNYC/**",
        "asos/NYC/**",
        "asos/knyc/**",
        "ASOS/KNYC/**",
        "raw/**/asos_obs/**",
    ):
        candidates.extend(sorted(data_root.glob(pattern)))
    return [path for path in candidates if path.is_file()]


def edt_midnight_hour_max_count(asos_or_note: Path | str | None = None) -> dict[str, Any]:
    """Count climate days whose daily max falls 00:00–01:00 local during EDT.

    Requires ASOS/METAR hourly maxima under ``data/`` for KNYC. Returns NOT_RUN
    when that corpus is absent rather than inventing counts.
    """
    del asos_or_note  # reserved hook; presence is determined from data/
    data_root = Path("data")
    asos_files = _asos_knyc_paths(data_root)
    if not asos_files:
        return {
            "status": "NOT_RUN",
            "reason": "no ASOS/METAR KNYC data under data/ (checked asos/KNYC and raw/**/asos_obs)",
        }

    try:
        from ingestion.climate_time import AsosObservation, asos_max_for_climate_day
    except ImportError as exc:
        return {
            "status": "NOT_RUN",
            "reason": f"ASOS helpers unavailable: {exc}",
        }

    observations: list[AsosObservation] = []
    for path in asos_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                ts = parts[0]
                tmpf = float(parts[2])
            except ValueError:
                continue

            stamp = ts.replace("Z", "+00:00")
            valid = datetime.fromisoformat(stamp)
            if valid.tzinfo is None:
                from datetime import timezone

                valid = valid.replace(tzinfo=timezone.utc)
            observations.append(AsosObservation(valid_utc=valid, tmpf=tmpf))

    if not observations:
        return {
            "status": "NOT_RUN",
            "reason": f"ASOS paths found ({len(asos_files)}) but no parseable observations",
            "paths_checked": [str(p) for p in asos_files[:20]],
        }

    ny = ZoneInfo("America/New_York")
    candidate_days = sorted({climate_day(obs.valid_utc, STATION) for obs in observations})
    counted_days = 0
    examples: list[dict[str, Any]] = []
    for day in candidate_days:
        bounds_start = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
        if not is_dst(bounds_start, STATION):
            continue
        max_f, max_ts = asos_max_for_climate_day(observations, day)
        if max_f is None or max_ts is None:
            continue
        civil = max_ts.astimezone(ny)
        if civil.hour != 0:
            continue
        counted_days += 1
        if len(examples) < 20:
            examples.append(
                {
                    "climate_day": day.isoformat(),
                    "max_f": max_f,
                    "max_local": civil.isoformat(),
                    "assigned_climate_day": climate_day(max_ts, STATION).isoformat(),
                }
            )

    return {
        "status": "OK",
        "n_climate_days_max_in_edt_midnight_hour": counted_days,
        "examples": examples,
        "note": (
            "Maxima between midnight and 1 AM local during EDT belong to the "
            "previous climate day under LST timeauth rules."
        ),
    }


def label_noise_report(
    clinyc_path: Path | str,
    *,
    start: date = SIX_BRACKET_ERA_START,
    end: date | None = None,
) -> dict[str, Any]:
    """Recompute CLINYC days with n_later > 0 at the era snapshot."""
    observations = read_clinyc(Path(clinyc_path))
    days = sorted({obs.climate_day for obs in observations if obs.is_full_day})
    if end is not None:
        days = [day for day in days if start <= day <= end]
    else:
        days = [day for day in days if day >= start]

    noisy_days: list[dict[str, Any]] = []
    for climate_day_value in days:
        high, n_later = high_at_snapshot(observations, climate_day_value)
        if n_later > 0:
            noisy_days.append(
                {
                    "climate_day": climate_day_value.isoformat(),
                    "high_at_snapshot": high,
                    "n_later": n_later,
                }
            )

    n_days = len(days)
    n_noisy = len(noisy_days)
    measured_rate = (n_noisy / n_days) if n_days else None
    return {
        "start": start.isoformat(),
        "end": (end.isoformat() if end is not None else None),
        "n_climate_days": n_days,
        "n_days_n_later_gt_0": n_noisy,
        "measured_rate": measured_rate,
        "recorded_k2_baseline_rate": RECORDED_LABEL_NOISE_RATE,
        "recorded_k2_baseline_fraction": "1/2416",
        "noisy_days": noisy_days[:100],
        "note": "Report measured and recorded rates; do not reconcile.",
    }
