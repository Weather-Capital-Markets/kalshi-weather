"""Resumable pagination, complement count-not-drop, cutoff trades_created_ts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.wxmm.test_c1_x1 import _payload
from wxmm.analysis.raw_store import CheckpointStore, ContentAddressedRawStore, TickerCheckpoint
from wxmm.analysis.trades_ingest import (
    ComplementTally,
    fetch_cutoff,
    paginate_trades,
    parquet_shard_paths,
    parse_cutoff,
    parse_trade,
    pull_ticker,
    trades_to_parquet,
)
from wxmm.core.errors import PriceComplementError


def test_assert_price_complement_still_raises_on_placeholder() -> None:
    with pytest.raises(PriceComplementError):
        parse_trade(_payload(yes="0.5600", no="0.5600"), source_endpoint="live")


def test_ingest_counts_complement_and_does_not_drop_ticker() -> None:
    tally = ComplementTally()
    good = parse_trade(
        _payload(trade_id="ok"),
        source_endpoint="historical",
        strict_complement=False,
        complement_tally=tally,
    )
    bad = parse_trade(
        _payload(trade_id="bad", yes="0.5600", no="0.5600"),
        source_endpoint="historical",
        strict_complement=False,
        complement_tally=tally,
    )
    assert good.trade_id == "ok"
    assert bad.trade_id == "bad"
    assert tally.n_checked == 2
    assert tally.n_violations == 1
    assert tally.worst_trade_id == "bad"
    assert tally.worst_deviation == Decimal("0.12")
    assert "bad" in tally.violating_ids


def test_cutoff_uses_trades_created_ts_for_partition() -> None:
    cutoff = parse_cutoff(
        {
            "market_settled_ts": "2026-07-16T00:00:00Z",
            "trades_created_ts": "2026-07-15T00:00:00Z",
        }
    )
    expected = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp())
    assert cutoff.trades_created_ts == expected
    assert cutoff.trade_partition_ts == expected
    assert cutoff.market_settled_ts != cutoff.trades_created_ts


def test_cutoff_falls_back_when_trades_created_ts_missing() -> None:
    cutoff = parse_cutoff({"market_settled_ts": 1_700_000_000})
    assert cutoff.trade_partition_ts == 1_700_000_000
    assert cutoff.trades_created_ts == 1_700_000_000


def test_paginate_resumes_after_mid_ticker_death(tmp_path: Path) -> None:
    pages = {
        None: {
            "trades": [_payload(trade_id="p1", created="2023-01-01T00:00:00Z")],
            "cursor": "next",
        },
        "next": {
            "trades": [_payload(trade_id="p2", created="2023-01-01T00:01:00Z")],
            "cursor": "",
        },
    }

    class Fake:
        def __init__(self) -> None:
            self.calls = 0

        def get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            self.calls += 1
            cursor = (params or {}).get("cursor")
            return pages[cursor]  # type: ignore[index, return-value]

    store = CheckpointStore(tmp_path / "ck")
    owner = TickerCheckpoint.fresh("KXHIGHNY-26AUG12-T90")

    def boom(page: int, _trades: object) -> None:
        if page == 1:
            raise RuntimeError("80 percent death")

    with pytest.raises(RuntimeError, match="80 percent"):
        paginate_trades(
            Fake(),  # type: ignore[arg-type]
            path="/historical/trades",
            source_endpoint="historical",
            ticker=owner.ticker,
            checkpoint=owner.historical,
            checkpoint_store=store,
            checkpoint_owner=owner,
            on_page=boom,
        )

    loaded = store.load(owner.ticker)
    assert loaded is not None
    assert loaded.historical.cursor == "next"
    assert loaded.historical.last_created_time is not None

    resumed = paginate_trades(
        Fake(),  # type: ignore[arg-type]
        path="/historical/trades",
        source_endpoint="historical",
        ticker=loaded.ticker,
        checkpoint=loaded.historical,
        checkpoint_store=store,
        checkpoint_owner=loaded,
    )
    assert [t.trade_id for t in resumed] == ["p1", "p2"]
    assert loaded.historical.done is True


def test_content_addressed_raw_store_writes_blob_before_index(tmp_path: Path) -> None:
    store = ContentAddressedRawStore(tmp_path / "raw")
    stored = store.put("https://example.test/trades?cursor=a", b'{"trades":[]}')
    assert stored.path.exists()
    assert stored.body_sha256 == stored.path.stem
    lines = store.index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "example.test" in lines[0]


def test_pull_ticker_uses_trades_created_ts() -> None:
    seen: list[tuple[str, object]] = []

    class Fake:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            params = params or {}
            seen.append((path, params.get("min_ts") or params.get("max_ts")))
            if path == "/historical/cutoff":
                return {"market_settled_ts": 100, "trades_created_ts": 200}
            return {"trades": [], "cursor": ""}

    cutoff = fetch_cutoff(Fake())  # type: ignore[arg-type]
    pull_ticker(Fake(), "KXHIGHNY-26AUG12-T90", cutoff)  # type: ignore[arg-type]
    assert cutoff.trade_partition_ts == 200
    assert ("/historical/trades", 200) in seen
    assert ("/markets/trades", 200) in seen


def test_parquet_shard_paths_prefers_monthly_files(tmp_path: Path) -> None:
    from tests.unit.wxmm.test_c1_x1 import _trade

    trade = _trade()
    monthly = tmp_path / "climate_month=2024-01.parquet"
    other = tmp_path / "other.parquet"
    trades_to_parquet([trade], monthly)
    trades_to_parquet([trade], other)
    paths = parquet_shard_paths(tmp_path)
    assert paths == [monthly]
    nested = tmp_path / "nested"
    tickers = nested / "_tickers"
    tickers.mkdir(parents=True)
    shard = tickers / "KXHIGHNY-24JAN01-T90.parquet"
    trades_to_parquet([trade], shard)
    assert parquet_shard_paths(nested) == [shard]
    assert parquet_shard_paths(monthly) == [monthly]
    assert parquet_shard_paths(tmp_path / "missing") == []
