"""CLOB API client for Polymarket order books (weather markets)."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from ingestion.client import RateLimiter, RequestAttempt, RequestResult

logger = logging.getLogger(__name__)


class PolymarketClobClient:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        clob = config.get("clob") or {}
        self.base_url = str(clob.get("base_url") or "https://clob.polymarket.com").rstrip("/")
        self.book_path = str(clob.get("book_path") or "/book")
        self.timeout_sec = float(clob.get("timeout_sec", 15))
        self.max_retries = int(clob.get("max_retries", 5))
        self._limiter = RateLimiter(float(clob.get("max_requests_per_sec", 15)))
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self.timeout_sec)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_book(self, token_id: str) -> RequestResult:
        return self.get(self.book_path, params={"token_id": token_id})

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> RequestResult:
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        attempts: list[RequestAttempt] = []
        while True:
            self._limiter.wait()
            started = time.perf_counter()
            try:
                response = self._client.get(url, params=params)
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
                self._backoff(attempt, response.status_code)
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
