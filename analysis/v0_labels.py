"""CLINYC-as-issued YES/NO labels for six-bracket KXHIGHNY/HIGHNY.

greater: high > floor. less: high < cap. between: floor ≤ high ≤ cap.
When the API omits strike_type on a T-suffix tail, the lower T on that
climate day is less and the higher T is greater. Do not treat every T as
greater — that double-counts the interior and invents a second winner.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from wxmm.analysis.maker_taker import SettlementLabel
from wxmm.analysis.trades_ingest import parse_climate_day
from wxmm.fairvalue.ladder import parse_kalshi_bracket


def _as_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    return int(raw)


def yes_won_from_high(
    strike_type: str, floor: int | None, cap: int | None, high: int
) -> bool:
    if strike_type == "greater":
        if floor is None:
            raise ValueError("greater strike missing floor")
        return high > floor
    if strike_type == "less":
        if cap is None:
            raise ValueError("less strike missing cap")
        return high < cap
    if strike_type == "between":
        if floor is None or cap is None:
            raise ValueError("between strike missing floor/cap")
        return floor <= high <= cap
    raise ValueError(f"unknown strike_type {strike_type!r}")


def resolve_strike(
    market: Mapping[str, Any],
    day_markets: list[Mapping[str, Any]],
) -> tuple[str, int | None, int | None]:
    strike = market.get("strike_type")
    floor = _as_int(market.get("floor_strike"))
    cap = _as_int(market.get("cap_strike"))
    if strike in {"greater", "less", "between"}:
        return str(strike), floor, cap
    parsed = parse_kalshi_bracket(str(market["ticker"]))
    if parsed is None:
        raise ValueError(f"cannot resolve strike for {market['ticker']!r}")
    if parsed.role == "between":
        return "between", parsed.floor_f, parsed.cap_f
    tails: list[int] = []
    for row in day_markets:
        other = parse_kalshi_bracket(str(row["ticker"]))
        if other is not None and other.role == "greater" and other.floor_f is not None:
            tails.append(other.floor_f)
    unique = sorted(set(tails))
    if len(unique) < 2 or parsed.floor_f is None:
        raise ValueError(f"cannot infer T-tail role for {market['ticker']!r}")
    if parsed.floor_f == unique[0]:
        return "less", None, parsed.floor_f
    if parsed.floor_f == unique[-1]:
        return "greater", parsed.floor_f, None
    raise ValueError(f"T-tail {market['ticker']!r} is neither min nor max on its day")


def labels_from_clinyc(
    markets: list[dict[str, Any]],
    clinyc: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, SettlementLabel], dict[str, Any]]:
    by_day: dict[date, list[dict[str, Any]]] = {}
    for market in markets:
        by_day.setdefault(parse_climate_day(str(market["ticker"])), []).append(market)

    labels: dict[str, SettlementLabel] = {}
    n_missing_clinyc = 0
    n_unresolved = 0
    n_inferred_tail = 0
    n_unique = 0
    n_not_unique = 0
    n_venue_agree = 0
    n_venue_disagree = 0
    not_unique_days: list[str] = []
    for climate, day_markets in sorted(by_day.items()):
        row = clinyc.get(climate.isoformat())
        if row is None or row.get("high_F") is None:
            n_missing_clinyc += 1
            continue
        high = int(row["high_F"])
        winners: list[str] = []
        pending: list[tuple[str, bool, str | None]] = []
        skipped = False
        for market in day_markets:
            try:
                inferred = market.get("strike_type") not in {"greater", "less", "between"}
                strike, floor, cap = resolve_strike(market, day_markets)
                if inferred and strike in {"greater", "less"}:
                    n_inferred_tail += 1
            except ValueError:
                n_unresolved += 1
                skipped = True
                break
            won = yes_won_from_high(strike, floor, cap, high)
            ticker = str(market["ticker"])
            pending.append((ticker, won, market.get("result") or None))
            if won:
                winners.append(ticker)
        if skipped:
            continue
        if len(winners) != 1:
            n_not_unique += 1
            not_unique_days.append(climate.isoformat())
            continue
        n_unique += 1
        for ticker, won, result in pending:
            labels[ticker] = SettlementLabel(
                ticker=ticker,
                climate_day=climate,
                yes_won=won,
                source="clinyc_as_issued",
            )
            if result in {"yes", "no"}:
                if (result == "yes") == won:
                    n_venue_agree += 1
                else:
                    n_venue_disagree += 1
    diagnostics = {
        "n_market_days": len(by_day),
        "n_missing_clinyc": n_missing_clinyc,
        "n_unresolved_strike": n_unresolved,
        "n_inferred_t_tails": n_inferred_tail,
        "n_unique_winner_days": n_unique,
        "n_not_unique_winner_days": n_not_unique,
        "not_unique_days": not_unique_days,
        "n_venue_result_agree": n_venue_agree,
        "n_venue_result_disagree": n_venue_disagree,
        "n_labels": len(labels),
    }
    return labels, diagnostics
