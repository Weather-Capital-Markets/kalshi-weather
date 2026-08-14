"""Persistent state: trades cursor, dedup, known markets."""

from __future__ import annotations

import sqlite3
from typing import Any

STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kv_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_trades (
  trade_id TEXT PRIMARY KEY,
  ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_markets (
  ticker TEXT PRIMARY KEY,
  last_status TEXT,
  first_seen_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL,
  settlement_captured INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_settlement (
  ticker TEXT PRIMARY KEY,
  queued_utc TEXT NOT NULL
);
"""


def init_state_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(STATE_SCHEMA_SQL)
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO kv_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def clear_state(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv_state WHERE key = ?", (key,))
    conn.commit()


def trade_cursor_key(series_ticker: str) -> str:
    return f"trades_cursor:{series_ticker}"


def extract_trade_ids(payload: dict[str, Any] | None) -> list[str]:
    """Extract trade IDs defensively from a trades API page."""
    if not payload or not isinstance(payload, dict):
        return []
    trades = payload.get("trades")
    if not isinstance(trades, list):
        return []
    ids: list[str] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        trade_id = trade.get("trade_id") or trade.get("id")
        if trade_id is not None:
            ids.append(str(trade_id))
    return ids


def mark_trades_seen(conn: sqlite3.Connection, trade_ids: list[str], ts_utc: str) -> None:
    for trade_id in trade_ids:
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_trades (trade_id, ts_utc) VALUES (?, ?)
            """,
            (trade_id, ts_utc),
        )
    conn.commit()


def filter_new_trade_ids(conn: sqlite3.Connection, trade_ids: list[str]) -> list[str]:
    if not trade_ids:
        return []
    placeholders = ",".join("?" for _ in trade_ids)
    rows = conn.execute(
        f"SELECT trade_id FROM seen_trades WHERE trade_id IN ({placeholders})",
        trade_ids,
    ).fetchall()
    seen = {row["trade_id"] for row in rows}
    return [trade_id for trade_id in trade_ids if trade_id not in seen]


def upsert_known_markets(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    ts_utc: str,
    status: str = "open",
) -> None:
    for ticker in tickers:
        conn.execute(
            """
            INSERT INTO known_markets (ticker, last_status, first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                last_status = excluded.last_status,
                last_seen_utc = excluded.last_seen_utc
            """,
            (ticker, status, ts_utc, ts_utc),
        )
    conn.commit()


def queue_dropped_markets(
    conn: sqlite3.Connection,
    *,
    previously_open: set[str],
    currently_open: set[str],
    ts_utc: str,
) -> list[str]:
    dropped = sorted(previously_open - currently_open)
    for ticker in dropped:
        conn.execute(
            "UPDATE known_markets SET last_status = 'pending_settlement' WHERE ticker = ?",
            (ticker,),
        )
        conn.execute(
            """
            INSERT INTO pending_settlement (ticker, queued_utc) VALUES (?, ?)
            ON CONFLICT(ticker) DO UPDATE SET queued_utc = excluded.queued_utc
            """,
            (ticker, ts_utc),
        )
    conn.commit()
    return dropped


def list_pending_settlement(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT ticker FROM pending_settlement ORDER BY queued_utc").fetchall()
    return [row["ticker"] for row in rows]


def mark_settlement_captured(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "UPDATE known_markets SET settlement_captured = 1 WHERE ticker = ?",
        (ticker,),
    )
    conn.execute("DELETE FROM pending_settlement WHERE ticker = ?", (ticker,))
    conn.commit()


def get_open_tickers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT ticker FROM known_markets
        WHERE last_status = 'open' AND settlement_captured = 0
        """
    ).fetchall()
    return {row["ticker"] for row in rows}
