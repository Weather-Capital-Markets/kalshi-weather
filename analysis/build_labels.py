"""Build per-ticker settlement labels from CLINYC as-issued + bracket metadata.

Labels are CLINYC as-issued only. METAR-derived maxima, GHCN-D, and "final"
climate data are forbidden by the prereg: they can disagree with what Kalshi
settled on.

Selection follows data-sources.md §1.3 with the era-dependent snapshot time of
venue-facts.md §1.10, via ``wxmm.settlement.eras.kalshi_snapshot_utc``. The
label is the latest issuance visible at that snapshot that covers the full
prior climate day. Later corrections are counted, never substituted.

Kalshi's own ``result`` field is read as a cross-check and reported as a
disagreement rate. It never overwrites the CLINYC label.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from analysis.pull_corpus import HttpTransport, enumerate_markets, in_scope
from wxmm.analysis.maker_taker import SettlementLabel
from wxmm.analysis.trades_ingest import SIX_BRACKET_ERA_START
from wxmm.core.utc import require_utc
from wxmm.settlement.brackets import strike_of, yes_won
from wxmm.settlement.eras import kalshi_rule_for, kalshi_snapshot_utc
from wxmm.settlement.rules import Observation

STATION = "KNYC"


def _role_of(market: Mapping[str, Any]) -> str:
    strike = strike_of(market)
    return "unparsed" if strike is None else strike.role


@dataclass(frozen=True, slots=True)
class LabelReport:
    n_markets: int
    n_labelled: int
    n_no_issuance: int
    n_missing_high: int
    n_unparsed_strike: int
    n_days_labelled: int
    n_revised_cli_days: int
    n_venue_compared: int
    n_venue_disagree: int
    disagreements: tuple[str, ...]

    @property
    def venue_disagreement_rate(self) -> float | None:
        if not self.n_venue_compared:
            return None
        return self.n_venue_disagree / self.n_venue_compared


def read_clinyc(path: Path) -> list[Observation]:
    """CSV rows from ``ingestion.cli_labels``. ``MM`` becomes ``high_f=None``."""
    out: list[Observation] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_high = (row.get("high_F") or "").strip()
            try:
                high: int | None = int(raw_high)
            except ValueError:
                high = None
            stamp = str(row["issuance_ts_utc"]).replace("Z", "+00:00")
            issued = require_utc(datetime.fromisoformat(stamp))
            climate = date.fromisoformat(row["climate_date"])
            same_day = str(row.get("is_same_day_intermediate", "")).strip().lower() == "true"
            out.append(
                Observation(
                    station=STATION,
                    climate_day=climate,
                    high_f=high,
                    valid_at=issued,
                    available_at=issued,
                    source="clinyc_as_issued",
                    is_full_day=not same_day,
                )
            )
    out.sort(key=lambda o: (o.climate_day, o.available_at))
    return out


def high_at_snapshot(
    observations: Sequence[Observation],
    climate_day: date,
) -> tuple[int | None, int]:
    """Latest full-day issuance visible at the era's snapshot. Returns (high, n_later)."""
    same_day = [o for o in observations if o.climate_day == climate_day]
    if not same_day:
        return None, 0
    rule = kalshi_rule_for(climate_day)
    snapshot = kalshi_snapshot_utc(climate_day, rule, tuple(same_day))
    visible = [o for o in same_day if o.is_full_day and o.available_at <= snapshot]
    later = [o for o in same_day if o.is_full_day and o.available_at > snapshot]
    if not visible:
        return None, len(later)
    chosen = max(visible, key=lambda o: o.available_at)
    return chosen.high_f, len(later)


def label_day_audit(
    observations: Sequence[Observation],
    climate_days: Sequence[date],
) -> dict[str, Any]:
    """Return lists: no_issuance_days, missing_high_days, revised_cli_days (n_later>0),
    and counts. Also compute label_noise_rate = days_with_revision / days_with_issuance.
    """
    no_issuance_days: list[str] = []
    missing_high_days: list[str] = []
    revised_cli_days: list[str] = []
    days_with_issuance = 0
    days_with_revision = 0

    for climate_day in sorted(set(climate_days)):
        same_day = [o for o in observations if o.climate_day == climate_day]
        if not same_day:
            no_issuance_days.append(climate_day.isoformat())
            continue
        high, n_later = high_at_snapshot(observations, climate_day)
        if high is None:
            missing_high_days.append(climate_day.isoformat())
        else:
            days_with_issuance += 1
            if n_later > 0:
                days_with_revision += 1
                revised_cli_days.append(climate_day.isoformat())

    label_noise_rate = (
        days_with_revision / days_with_issuance if days_with_issuance else None
    )
    return {
        "no_issuance_days": no_issuance_days,
        "missing_high_days": missing_high_days,
        "revised_cli_days": revised_cli_days,
        "n_no_issuance": len(no_issuance_days),
        "n_missing_high": len(missing_high_days),
        "n_revised_cli": len(revised_cli_days),
        "days_with_issuance": days_with_issuance,
        "days_with_revision": days_with_revision,
        "label_noise_rate": label_noise_rate,
    }


def write_label_day_audit(audit: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(audit), indent=2), encoding="utf-8")


def build_labels(
    markets: Sequence[Mapping[str, Any]],
    observations: Sequence[Observation],
    *,
    start: date,
    end: date,
) -> tuple[dict[str, SettlementLabel], LabelReport]:
    highs: dict[date, int | None] = {}
    n_later_by_day: dict[date, int] = {}
    labels: dict[str, SettlementLabel] = {}
    n_no_issuance = 0
    n_missing_high = 0
    n_unparsed = 0
    n_compared = 0
    n_disagree = 0
    disagreements: list[str] = []
    considered = 0

    for market in markets:
        ticker = str(market.get("ticker") or "")
        climate = in_scope(ticker, start=start, end=end)
        if climate is None:
            continue
        considered += 1
        if climate not in highs:
            high, n_later = high_at_snapshot(observations, climate)
            highs[climate] = high
            n_later_by_day[climate] = n_later
        high = highs[climate]
        if high is None:
            if not any(o.climate_day == climate for o in observations):
                n_no_issuance += 1
            else:
                n_missing_high += 1
            continue
        strike = strike_of(market)
        if strike is None:
            n_unparsed += 1
            continue
        won = yes_won(strike, high)
        labels[ticker] = SettlementLabel(
            ticker=ticker,
            climate_day=climate,
            yes_won=won,
            source="clinyc_as_issued",
        )
        venue = str(market.get("result") or "").strip().lower()
        if venue in {"yes", "no"}:
            n_compared += 1
            if (venue == "yes") != won:
                n_disagree += 1
                if len(disagreements) < 200:
                    disagreements.append(f"{ticker} clinyc={won} venue={venue} high={high}")

    n_revised_cli_days = sum(
        1
        for day, later in n_later_by_day.items()
        if later > 0 and highs.get(day) is not None
    )
    report = LabelReport(
        n_markets=considered,
        n_labelled=len(labels),
        n_no_issuance=n_no_issuance,
        n_missing_high=n_missing_high,
        n_unparsed_strike=n_unparsed,
        n_days_labelled=len({label.climate_day for label in labels.values()}),
        n_revised_cli_days=n_revised_cli_days,
        n_venue_compared=n_compared,
        n_venue_disagree=n_disagree,
        disagreements=tuple(disagreements),
    )
    return labels, report


def write_labels(labels: Mapping[str, SettlementLabel], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ticker": label.ticker,
            "climate_day": label.climate_day.isoformat(),
            "yes_won": label.yes_won,
            "source": label.source,
        }
        for label in sorted(labels.values(), key=lambda x: (x.climate_day, x.ticker))
    ]
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def read_labels(path: Path) -> dict[str, SettlementLabel]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, SettlementLabel] = {}
    for row in rows:
        out[str(row["ticker"])] = SettlementLabel(
            ticker=str(row["ticker"]),
            climate_day=date.fromisoformat(str(row["climate_day"])),
            yes_won=bool(row["yes_won"]),
            source="clinyc_as_issued",
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinyc", type=Path, default=Path("data/labels/clinyc.csv"))
    parser.add_argument("--markets-json", type=Path, default=None)
    parser.add_argument("--series", nargs="+", default=["KXHIGHNY"])
    parser.add_argument("--start", type=date.fromisoformat, default=SIX_BRACKET_ERA_START)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out", type=Path, default=Path("data/labels/settlement.json"))
    parser.add_argument(
        "--audit-out",
        type=Path,
        default=None,
        help="Optional path for label_day_audit JSON (v0_run day lists)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.clinyc.exists():
        print(f"no CLINYC csv at {args.clinyc}; run ingestion.cli_labels", file=sys.stderr)
        return 2
    observations = read_clinyc(args.clinyc)
    print(f"clinyc issuances {len(observations)}")

    if args.markets_json is not None and args.markets_json.exists():
        markets = json.loads(args.markets_json.read_text(encoding="utf-8"))
    else:
        transport = HttpTransport()
        markets = []
        for series in args.series:
            markets.extend(enumerate_markets(transport, series))
        if args.markets_json is not None:
            args.markets_json.parent.mkdir(parents=True, exist_ok=True)
            args.markets_json.write_text(json.dumps(markets), encoding="utf-8")
    print(f"markets {len(markets)}")

    labels, report = build_labels(
        markets, observations, start=args.start, end=args.end
    )
    write_labels(labels, args.out)
    if args.audit_out is not None:
        climate_days = sorted(
            {
                climate
                for market in markets
                if (
                    climate := in_scope(
                        str(market.get("ticker") or ""),
                        start=args.start,
                        end=args.end,
                    )
                )
                is not None
            }
        )
        write_label_day_audit(
            label_day_audit(observations, climate_days),
            args.audit_out,
        )
    payload = {
        "n_markets": report.n_markets,
        "n_labelled": report.n_labelled,
        "n_no_issuance": report.n_no_issuance,
        "n_missing_high": report.n_missing_high,
        "n_unparsed_strike": report.n_unparsed_strike,
        "n_days_labelled": report.n_days_labelled,
        "n_revised_cli_days": report.n_revised_cli_days,
        "n_venue_compared": report.n_venue_compared,
        "n_venue_disagree": report.n_venue_disagree,
        "venue_disagreement_rate": report.venue_disagreement_rate,
        "roles": dict(Counter(_role_of(m) for m in markets)),
    }
    print(json.dumps(payload, indent=2))
    if report.disagreements:
        print("\nfirst venue disagreements:")
        for line in report.disagreements[:10]:
            print(f"  {line}")
    print(f"\nwrote {args.out}")
    return 0 if report.n_labelled else 1


if __name__ == "__main__":
    raise SystemExit(main())
