"""Gamma API discovery for Polymarket NYC daily-high temperature events."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ingestion.client import RateLimiter, RequestAttempt, RequestResult

logger = logging.getLogger(__name__)

_STRIKE_RE_HIGHER = re.compile(r"(\d+)\s*(?:°|º)?F?\s*or higher", re.I)
_STRIKE_RE_BELOW = re.compile(r"(\d+)\s*(?:°|º)?F?\s*or below", re.I)
_STRIKE_RE_BETWEEN = re.compile(r"between\s+(\d+)\s*[-–]\s*(\d+)", re.I)
_STRIKE_RE_BIN = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*(?:°|º)?F", re.I)


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict)]
    return []


class PolymarketGammaClient:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        gamma = config.get("gamma") or {}
        self.base_url = str(gamma.get("base_url") or "https://gamma-api.polymarket.com").rstrip("/")
        self.timeout_sec = float(gamma.get("timeout_sec", 15))
        self.max_retries = int(gamma.get("max_retries", 3))
        self._limiter = RateLimiter(float(gamma.get("max_requests_per_sec", 10)))
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self.timeout_sec)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> RequestResult:
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        attempts: list[RequestAttempt] = []
        while True:
            self._limiter.wait()
            started = __import__("time").perf_counter()
            try:
                response = self._client.get(url, params=params)
                latency_ms = int((__import__("time").perf_counter() - started) * 1000)
            except httpx.RequestError as exc:
                latency_ms = int((__import__("time").perf_counter() - started) * 1000)
                attempts.append(
                    RequestAttempt(status_code=None, latency_ms=latency_ms, error_text=str(exc))
                )
                if attempt >= self.max_retries:
                    return RequestResult(
                        status_code=None,
                        latency_ms=latency_ms,
                        json_body=None,
                        error_text=str(exc),
                        endpoint=path,
                        attempts=tuple(attempts),
                        text_body=None,
                    )
                attempt += 1
                continue

            text_body = response.text
            json_body = None
            error_text = None
            try:
                json_body = response.json()
            except ValueError:
                error_text = text_body[:500] if text_body else "non-json response"
            if not response.is_success and error_text is None:
                error_text = text_body[:500] if text_body else f"HTTP {response.status_code}"

            attempts.append(
                RequestAttempt(
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    error_text=error_text,
                )
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    return RequestResult(
                        status_code=response.status_code,
                        latency_ms=latency_ms,
                        json_body=json_body if isinstance(json_body, (dict, list)) else None,
                        error_text=error_text,
                        endpoint=path,
                        attempts=tuple(attempts),
                        text_body=text_body,
                    )
                attempt += 1
                continue

            return RequestResult(
                status_code=response.status_code,
                latency_ms=latency_ms,
                json_body=json_body if isinstance(json_body, (dict, list)) else None,
                error_text=error_text,
                endpoint=path,
                attempts=tuple(attempts),
                text_body=text_body,
            )

    def list_series_events(
        self,
        series_slug: str,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 10,
        order: str = "endDate",
        ascending: bool = True,
    ) -> RequestResult:
        return self.get(
            "/events",
            params={
                "series_slug": series_slug,
                "active": active,
                "closed": closed,
                "limit": limit,
                "order": order,
                "ascending": ascending,
            },
        )

    def event_by_slug(self, slug: str) -> RequestResult:
        return self.get(f"/events/slug/{slug}")


def event_slug_candidates(
    day: date,
    *,
    prefix: str = "highest-temperature-in-nyc-on",
) -> list[str]:
    month = day.strftime("%B").lower()
    day_num = day.day
    year = day.year
    return [
        f"{prefix}-{month}-{day_num}-{year}",
        f"{prefix}-{month}-{day_num}",
    ]


def resolve_event_for_day(
    client: PolymarketGammaClient,
    day: date,
    *,
    prefix: str = "highest-temperature-in-nyc-on",
) -> tuple[str | None, RequestResult | None, dict[str, Any] | None]:
    for slug in event_slug_candidates(day, prefix=prefix):
        result = client.event_by_slug(slug)
        if result.ok and isinstance(result.json_body, dict):
            return slug, result, result.json_body
    return None, None, None


def pick_current_and_next_events(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    current = now or datetime.now(timezone.utc)
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        end_raw = event.get("endDate")
        if not isinstance(end_raw, str):
            continue
        try:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        dated.append((end_dt, event))
    dated.sort(key=lambda item: item[0])
    future_or_today = [
        event
        for end_dt, event in dated
        if end_dt >= current.replace(hour=0, minute=0, second=0, microsecond=0)
    ]
    if not future_or_today:
        future_or_today = [event for _, event in dated[-2:]]
    current_event = future_or_today[0] if future_or_today else None
    next_event = future_or_today[1] if len(future_or_today) > 1 else None
    return current_event, next_event


def bracket_labels(markets: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for market in markets:
        title = market.get("groupItemTitle")
        if isinstance(title, str) and title.strip():
            labels.append(title.strip())
        else:
            labels.append(str(market.get("question") or market.get("slug") or ""))
    return labels


def parse_bracket(market: dict[str, Any]) -> dict[str, Any] | None:
    """Parse disjoint Fahrenheit bracket metadata (not cumulative >= thresholds)."""
    label = market.get("groupItemTitle")
    if isinstance(label, str) and label.strip():
        label_text = label.strip()
    else:
        label_text = ""
    for text in (label_text, market.get("question")):
        if not isinstance(text, str) or not text.strip():
            continue
        match = _STRIKE_RE_HIGHER.search(text)
        if match:
            low = int(match.group(1))
            bracket_label = label_text or f"{low}°F or higher"
            return {
                "bracket_kind": "tail_above",
                "bracket_low_f": low,
                "bracket_high_f": None,
                "bracket_label": bracket_label,
            }
        match = _STRIKE_RE_BELOW.search(text)
        if match:
            high = int(match.group(1))
            bracket_label = label_text or f"{high}°F or below"
            return {
                "bracket_kind": "tail_below",
                "bracket_low_f": None,
                "bracket_high_f": high,
                "bracket_label": bracket_label,
            }
        match = _STRIKE_RE_BETWEEN.search(text)
        if match:
            low = int(match.group(1))
            high = int(match.group(2))
            bracket_label = label_text or f"{low}-{high}°F"
            return {
                "bracket_kind": "between",
                "bracket_low_f": low,
                "bracket_high_f": high,
                "bracket_label": bracket_label,
            }
        match = _STRIKE_RE_BIN.search(text)
        if match:
            low = int(match.group(1))
            high = int(match.group(2))
            bracket_label = label_text or f"{low}-{high}°F"
            return {
                "bracket_kind": "between",
                "bracket_low_f": low,
                "bracket_high_f": high,
                "bracket_label": bracket_label,
            }
    return None


def yes_token_id(market: dict[str, Any]) -> str | None:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return str(parsed[0])
        except json.JSONDecodeError:
            return None
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return None


def is_open_bracket_market(market: dict[str, Any]) -> bool:
    if market.get("closed"):
        return False
    if market.get("active") is False:
        return False
    if market.get("acceptingOrders") is False:
        return False
    return parse_bracket(market) is not None


def enrich_market(market: dict[str, Any], *, event_slug: str | None = None) -> dict[str, Any]:
    bracket = parse_bracket(market)
    slug = market.get("slug")
    pm_meta: dict[str, Any] = {
        "event_slug": event_slug,
        "market_slug": slug if isinstance(slug, str) else None,
        "yes_token_id": yes_token_id(market),
        "neg_risk": market.get("negRisk"),
    }
    if bracket is not None:
        pm_meta.update(bracket)
    return {
        **market,
        "pm_meta": pm_meta,
    }


def open_bracket_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if not isinstance(markets, list):
        return []
    event_slug = event.get("slug") if isinstance(event.get("slug"), str) else None
    open_markets = [m for m in markets if isinstance(m, dict) and is_open_bracket_market(m)]
    return [enrich_market(m, event_slug=event_slug) for m in open_markets]


def bracket_ladder_set(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        meta = market.get("pm_meta") if isinstance(market.get("pm_meta"), dict) else {}
        kind = meta.get("bracket_kind")
        if not isinstance(kind, str):
            continue
        rows.append(
            {
                "bracket_kind": kind,
                "bracket_low_f": meta.get("bracket_low_f"),
                "bracket_high_f": meta.get("bracket_high_f"),
                "bracket_label": meta.get("bracket_label"),
                "market_slug": meta.get("market_slug") or market.get("slug"),
                "neg_risk": meta.get("neg_risk"),
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[int, int]:
        low = row.get("bracket_low_f")
        high = row.get("bracket_high_f")
        low_sort = low if isinstance(low, int) else -1
        high_sort = high if isinstance(high, int) else 999
        return (low_sort, high_sort)

    rows.sort(key=sort_key)
    return rows


# Back-compat aliases
open_threshold_markets = open_bracket_markets
ladder_strike_set = bracket_ladder_set


def discover_daily_events(
    client: PolymarketGammaClient,
    *,
    series_slug: str,
    event_prefix: str,
    horizon_days: int,
    discovery_limit: int,
    now: datetime | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[RequestResult]]:
    """Return (event_slug, event) pairs and all Gamma HTTP results for heartbeat."""
    current = now or datetime.now(timezone.utc)
    today = current.date()
    discovered: list[tuple[str, dict[str, Any]]] = []
    seen_slugs: set[str] = set()
    request_results: list[RequestResult] = []

    series_result = client.list_series_events(series_slug, limit=discovery_limit)
    request_results.append(series_result)
    series_events = _extract_events(series_result.json_body)
    for day_offset in range(max(horizon_days, 1)):
        day = today + timedelta(days=day_offset)
        for slug in event_slug_candidates(day, prefix=event_prefix):
            if slug in seen_slugs:
                continue
            result = client.event_by_slug(slug)
            request_results.append(result)
            if result.ok and isinstance(result.json_body, dict):
                discovered.append((slug, result.json_body))
                seen_slugs.add(slug)
                break

    if not discovered and series_events:
        picked_current, picked_next = pick_current_and_next_events(series_events, now=current)
        for event in (picked_current, picked_next):
            if not isinstance(event, dict):
                continue
            slug = event.get("slug")
            if not isinstance(slug, str) or slug in seen_slugs:
                continue
            full = client.event_by_slug(slug)
            request_results.append(full)
            if full.ok and isinstance(full.json_body, dict):
                discovered.append((slug, full.json_body))
                seen_slugs.add(slug)

    return discovered, request_results
