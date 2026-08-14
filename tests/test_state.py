"""Tests for persistent state (cursor, dedup, settlement queue)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.heartbeat import connect
from ingestion.state import (
    extract_trade_ids,
    filter_new_trade_ids,
    get_state,
    init_state_schema,
    list_pending_settlement,
    mark_trades_seen,
    queue_dropped_markets,
    set_state,
    trade_cursor_key,
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


def test_settlement_queue(conn) -> None:
    queue_dropped_markets(
        conn,
        previously_open={"KXHIGHNY-T90", "KXHIGHNY-T91"},
        currently_open={"KXHIGHNY-T91"},
        ts_utc="2026-08-14T12:00:00.000Z",
    )
    assert list_pending_settlement(conn) == ["KXHIGHNY-T90"]
