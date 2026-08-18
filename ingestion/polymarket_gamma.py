"""Gamma API discovery for Polymarket NYC daily-high temperature events."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ingestion.client import RateLimiter, RequestAttempt, RequestResult

logger = logging.getLogger(__name__)


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
