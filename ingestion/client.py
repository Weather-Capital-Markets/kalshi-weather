"""Synchronous HTTP client for Kalshi Trade API v2."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestResult:
    status_code: int | None
    latency_ms: int
    json_body: dict[str, Any] | list[Any] | None
    error_text: str | None
    endpoint: str
    attempts: tuple[RequestAttempt, ...] = ()
    text_body: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@dataclass(frozen=True)
class RequestAttempt:
    status_code: int | None
    latency_ms: int
    error_text: str | None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, max_requests_per_sec: float) -> None:
        self._interval = 1.0 / max_requests_per_sec if max_requests_per_sec > 0 else 0.0
        self._last_request = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_request = time.monotonic()


class AuthStub:
    """Optional auth headers for future authenticated endpoints."""

    def __init__(self, api_key_id: str | None, private_key_path: str | None) -> None:
        self.api_key_id = (api_key_id or "").strip() or None
        self.private_key_path = (private_key_path or "").strip() or None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)

    def headers(self) -> dict[str, str]:
        # TODO(verify): implement Kalshi request signing when auth is needed.
        if not self.enabled:
            return {}
        logger.warning("auth stub enabled but signing not implemented")
        return {}


class KalshiClient:
    def __init__(
        self,
        config: dict[str, Any],
        auth: AuthStub | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        api = config["api"]
        self.base_url = api["base_url"].rstrip("/") + "/"
        self.paths = api["paths"]
        self.timeout_sec = float(api.get("timeout_sec", 15))
        self.max_retries = int(api.get("max_retries", 5))
        self.orderbook_depth = int(api.get("orderbook_depth", 0))
        self.trades_page_limit = int(api.get("trades_page_limit", 1000))
        self._limiter = RateLimiter(float(api.get("max_requests_per_sec", 5)))
        self._auth = auth or AuthStub(None, None)
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self.timeout_sec)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def path(self, name: str, **kwargs: str) -> str:
        template = self.paths[name]
        return template.format(**kwargs)

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> RequestResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        attempt = 0
        attempts: list[RequestAttempt] = []
        while True:
            self._limiter.wait()
            started = time.perf_counter()
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers=self._auth.headers(),
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
            except httpx.RequestError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                attempts.append(
                    RequestAttempt(
                        status_code=None,
                        latency_ms=latency_ms,
                        error_text=str(exc),
                    )
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
                self._backoff(attempt)
                attempt += 1
                continue

            response_result = self._result_from_response(response, path, latency_ms)
            attempts.append(
                RequestAttempt(
                    status_code=response_result.status_code,
                    latency_ms=response_result.latency_ms,
                    error_text=response_result.error_text,
                )
            )

            if response.status_code == 401:
                logger.warning("HTTP 401 on %s (continuing without auth)", path)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    return RequestResult(
                        status_code=response_result.status_code,
                        latency_ms=response_result.latency_ms,
                        json_body=response_result.json_body,
                        error_text=response_result.error_text,
                        endpoint=path,
                        attempts=tuple(attempts),
                        text_body=response_result.text_body,
                    )
                self._backoff(attempt, response.status_code)
                attempt += 1
                continue

            return RequestResult(
                status_code=response_result.status_code,
                latency_ms=response_result.latency_ms,
                json_body=response_result.json_body,
                error_text=response_result.error_text,
                endpoint=path,
                attempts=tuple(attempts),
                text_body=response_result.text_body,
            )

    def _result_from_response(
        self,
        response: httpx.Response,
        path: str,
        latency_ms: int,
    ) -> RequestResult:
        error_text = None
        json_body = None
        text_body = response.text
        try:
            json_body = response.json()
        except ValueError:
            error_text = text_body[:500] if text_body else "non-json response"
        if not response.is_success and error_text is None:
            error_text = text_body[:500] if text_body else f"HTTP {response.status_code}"
        return RequestResult(
            status_code=response.status_code,
            latency_ms=latency_ms,
            json_body=json_body if isinstance(json_body, (dict, list)) else None,
            error_text=error_text,
            endpoint=path,
            text_body=text_body,
        )

    @staticmethod
    def _backoff(attempt: int, status_code: int | None = None) -> None:
        base = 0.5 * (2**attempt)
        jitter = random.uniform(0, 0.25)
        delay = base + jitter
        logger.warning(
            "backing off %.2fs after attempt %s (status=%s)",
            delay,
            attempt + 1,
            status_code,
        )
        time.sleep(delay)
