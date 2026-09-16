"""HTTP transport: 429 backoff, 401 credential upgrade, HIGHNY default series."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from datetime import date

import pytest

from analysis.pull_corpus import DEFAULT_SERIES, HttpTransport, in_scope, looks_like_highny
from wxmm.core.errors import CredentialsUnavailable


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _http_error(url: str, code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    fp = io.BytesIO(b'{"error":"too many requests"}')
    return urllib.error.HTTPError(url, code, "error", headers, fp)


def test_default_series_includes_legacy_highny() -> None:
    assert DEFAULT_SERIES == ("KXHIGHNY", "HIGHNY")
    assert looks_like_highny("HIGHNY-22DEC11-B38.5")
    assert in_scope("HIGHNY-22DEC11-T38", start=date(2022, 12, 11), end=date(2022, 12, 12))


def test_429_retries_with_backoff_and_succeeds() -> None:
    calls = {"n": 0}

    def opener(request: object, timeout: float = 0) -> _FakeResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error("https://example.test/historical/trades", 429, retry_after="0")
        return _FakeResponse(json.dumps({"trades": [], "cursor": ""}).encode())

    transport = HttpTransport(min_interval=0.0, opener=opener, max_retries=3)
    body = transport.get_json("/historical/trades", {"ticker": "KXHIGHNY-26AUG12-T90"})
    assert body["trades"] == []
    assert transport.n_429 == 1
    assert calls["n"] == 2


def test_401_without_env_credentials_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WXMM_KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("WXMM_KALSHI_API_SECRET", raising=False)

    def opener(request: object, timeout: float = 0) -> _FakeResponse:
        raise _http_error("https://example.test/markets/trades", 401)

    transport = HttpTransport(min_interval=0.0, opener=opener, max_retries=2)
    with pytest.raises(CredentialsUnavailable):
        transport.get_json("/markets/trades")
