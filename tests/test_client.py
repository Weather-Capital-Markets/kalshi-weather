"""Tests for HTTP retry accounting."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from ingestion.client import KalshiClient


def test_retry_result_contains_every_physical_attempt() -> None:
    responses = iter(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"markets": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    config = {
        "api": {
            "base_url": "https://example.test/trade-api/v2",
            "paths": {"markets": "/markets"},
            "max_requests_per_sec": 0,
            "max_retries": 1,
        }
    }
    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(transport=transport) as http_client,
        patch("ingestion.client.KalshiClient._backoff"),
    ):
        client = KalshiClient(config, http_client=http_client)
        result = client.get("/markets")

    assert result.ok
    assert [attempt.status_code for attempt in result.attempts] == [429, 200]
