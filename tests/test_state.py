"""Tests for persistent state (cursor, dedup, settlement queue)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.heartbeat import connect
from ingestion.state import (
    extract_trade_ids,
    filter_new_trade_ids,
    get_state,
    init_backfill_schema,
    init_state_schema,
    list_history_markets,
    list_pending_settlement,
    mark_trades_seen,
    queue_dropped_markets,
    set_state,
    trade_cursor_key,
    upsert_history_market,
)


@pytest.fixture
def conn(tmp_path: Path):
    db = connect(tmp_path / "state.sqlite")
    init_state_schema(db)
    yield db
    db.close()


def test_trade_cursor_persists(conn) -> None:
    key = trade_cursor_key("KXHIGHNY")
    set_state(conn, key, "cursor-abc")
    assert get_state(conn, key) == "cursor-abc"


def test_trade_dedup(conn) -> None:
    payload = {
        "trades": [
            {"trade_id": "t1", "ticker": "KXHIGHNY-T90"},
            {"id": "t2", "ticker": "KXHIGHNY-T91"},
        ]
    }
    ids = extract_trade_ids(payload)
    assert ids == ["t1", "t2"]
    assert filter_new_trade_ids(conn, ids) == ["t1", "t2"]
    mark_trades_seen(conn, ["t1"], "2026-08-14T12:00:00.000Z")
    assert filter_new_trade_ids(conn, ids) == ["t2"]


def test_settlement_ts_column_is_added_to_a_preexisting_database(tmp_path: Path) -> None:
    # The dry-run already created backfill.sqlite, where CREATE TABLE IF NOT
    # EXISTS cannot add the column.
    db = connect(tmp_path / "backfill.sqlite")
    db.executescript(
        """
        CREATE TABLE history_markets (
          ticker TEXT PRIMARY KEY,
          series_ticker TEXT,
          open_time TEXT,
          close_time TEXT,
          status TEXT,
          enumerated_utc TEXT NOT NULL
        );
        """
    )
    db.commit()
    init_backfill_schema(db)
    init_backfill_schema(db)
    upsert_history_market(
        db,
        ticker="HIGHNY-24AUG15-T83",
        series_ticker="HIGHNY",
        open_time="2024-08-14T14:00:00Z",
        close_time="2024-08-16T03:59:00Z",
        status="settled",
        enumerated_utc="2026-08-14T12:00:00.000Z",
        settlement_ts="2024-08-16T04:02:00Z",
    )
    rows = list_history_markets(db)
    assert rows[0]["settlement_ts"] == "2024-08-16T04:02:00Z"
    db.close()


def test_upsert_history_market_does_not_clobber_with_nulls(tmp_path: Path) -> None:
    db = connect(tmp_path / "backfill.sqlite")
    init_backfill_schema(db)
    upsert_history_market(
        db,
        ticker="HIGHNY-24AUG15-T83",
        series_ticker="HIGHNY",
        open_time="2024-08-14T14:00:00Z",
        close_time="2024-08-16T03:59:00Z",
        status="settled",
        enumerated_utc="2026-08-14T12:00:00.000Z",
        settlement_ts="2024-08-16T04:02:00Z",
    )
    upsert_history_market(
        db,
        ticker="HIGHNY-24AUG15-T83",
        series_ticker="HIGHNY",
        open_time=None,
        close_time=None,
        status="settled",
        enumerated_utc="2026-08-14T13:00:00.000Z",
        settlement_ts=None,
    )
    row = list_history_markets(db)[0]
    assert row["open_time"] == "2024-08-14T14:00:00Z"
    assert row["close_time"] == "2024-08-16T03:59:00Z"
    assert row["settlement_ts"] == "2024-08-16T04:02:00Z"
    db.close()


def test_settlement_queue(conn) -> None:
    queue_dropped_markets(
        conn,
        previously_open={"KXHIGHNY-T90", "KXHIGHNY-T91"},
        currently_open={"KXHIGHNY-T91"},
        ts_utc="2026-08-14T12:00:00.000Z",
    )
    assert list_pending_settlement(conn) == ["KXHIGHNY-T90"]
