"""Tests for Polymarket CLOB client."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from ingestion.polymarket_clob import PolymarketClobClient


def test_clob_get_book_returns_json() -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = '{"bids":[],"asks":[]}'
    response.json.return_value = {"bids": [], "asks": []}
    response.is_success = True

    client = httpx.Client()
    client.get = MagicMock(return_value=response)
    clob = PolymarketClobClient(
        {"clob": {"base_url": "https://clob.polymarket.com", "max_requests_per_sec": 100}},
        http_client=client,
    )
    result = clob.get_book("token-123")
    clob.close()
    assert result.ok
    assert result.status_code == 200
    assert result.json_body == {"bids": [], "asks": []}
    client.get.assert_called_once()
    call_url = client.get.call_args[0][0]
    assert "clob.polymarket.com/book" in call_url
    assert client.get.call_args[1]["params"] == {"token_id": "token-123"}
