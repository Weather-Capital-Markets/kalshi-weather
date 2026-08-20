"""Persistent sqlite helpers.

Callers choose the file. Live logger passes heartbeat.sqlite; backfill tools
pass backfill.sqlite. This module never opens a path on its own.
"""

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


BACKFILL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS history_markets (
  ticker TEXT PRIMARY KEY,
  series_ticker TEXT,
  open_time TEXT,
  close_time TEXT,
  settlement_ts TEXT,
  status TEXT,
  enumerated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candlestick_progress (
  ticker TEXT PRIMARY KEY,
  last_end_ts INTEGER NOT NULL,
  complete INTEGER NOT NULL DEFAULT 0,
  updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cli_month_progress (
  month TEXT PRIMARY KEY,
  complete INTEGER NOT NULL DEFAULT 0,
  updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asos_month_progress (
  station TEXT NOT NULL,
  month TEXT NOT NULL,
  complete INTEGER NOT NULL DEFAULT 0,
  updated_utc TEXT NOT NULL,
  PRIMARY KEY (station, month)
);
CREATE TABLE IF NOT EXISTS nbm_climate_day_progress (
  climate_date TEXT PRIMARY KEY,
  complete INTEGER NOT NULL DEFAULT 0,
  updated_utc TEXT NOT NULL
);
"""


BACKFILL_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("history_markets", "settlement_ts", "TEXT"),
)


def _migrate_asos_month_progress(conn: sqlite3.Connection) -> None:
    """Upgrade legacy month-only progress rows to (station, month) keys."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(asos_month_progress)")}
    if not columns:
        return
    if "station" in columns:
        return
    conn.execute(
        """
        CREATE TABLE asos_month_progress_v2 (
          station TEXT NOT NULL,
          month TEXT NOT NULL,
          complete INTEGER NOT NULL DEFAULT 0,
          updated_utc TEXT NOT NULL,
          PRIMARY KEY (station, month)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO asos_month_progress_v2 (station, month, complete, updated_utc)
        SELECT 'NYC', month, complete, updated_utc FROM asos_month_progress
        """
    )
    conn.execute("DROP TABLE asos_month_progress")
    conn.execute("ALTER TABLE asos_month_progress_v2 RENAME TO asos_month_progress")
    conn.commit()


def init_backfill_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BACKFILL_SCHEMA_SQL)
    _migrate_asos_month_progress(conn)
    # CREATE TABLE IF NOT EXISTS is a no-op on a database written by an earlier
    # schema, so columns added later need an explicit ALTER.
    for table, column, decl in BACKFILL_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def nbm_day_complete(conn: sqlite3.Connection, climate_date: str) -> bool:
    row = conn.execute(
        "SELECT complete FROM nbm_climate_day_progress WHERE climate_date = ?",
        (climate_date,),
    ).fetchone()
    return bool(row and row["complete"])


def set_nbm_day_complete(conn: sqlite3.Connection, climate_date: str, updated_utc: str) -> None:
    conn.execute(
        """
        INSERT INTO nbm_climate_day_progress (climate_date, complete, updated_utc)
        VALUES (?, 1, ?)
        ON CONFLICT(climate_date) DO UPDATE SET
            complete = 1,
            updated_utc = excluded.updated_utc
        """,
        (climate_date, updated_utc),
    )
    conn.commit()


def upsert_history_market(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    series_ticker: str,
    open_time: str | None,
    close_time: str | None,
    status: str | None,
    enumerated_utc: str,
    settlement_ts: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO history_markets (
            ticker, series_ticker, open_time, close_time, settlement_ts,
            status, enumerated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            series_ticker = excluded.series_ticker,
            open_time = excluded.open_time,
            close_time = excluded.close_time,
            settlement_ts = excluded.settlement_ts,
            status = excluded.status,
            enumerated_utc = excluded.enumerated_utc
        """,
        (
            ticker,
            series_ticker,
            open_time,
            close_time,
            settlement_ts,
            status,
            enumerated_utc,
        ),
    )
    conn.commit()


def list_history_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ticker, series_ticker, open_time, close_time, settlement_ts, status "
        "FROM history_markets"
    ).fetchall()


def get_candle_progress(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ticker, last_end_ts, complete, updated_utc "
        "FROM candlestick_progress WHERE ticker = ?",
        (ticker,),
    ).fetchone()


def set_candle_progress(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    last_end_ts: int,
    complete: bool,
    updated_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO candlestick_progress (ticker, last_end_ts, complete, updated_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            last_end_ts = excluded.last_end_ts,
            complete = excluded.complete,
            updated_utc = excluded.updated_utc
        """,
        (ticker, last_end_ts, 1 if complete else 0, updated_utc),
    )
    conn.commit()


def month_complete(conn: sqlite3.Connection, month: str) -> bool:
    row = conn.execute(
        "SELECT complete FROM cli_month_progress WHERE month = ?",
        (month,),
    ).fetchone()
    return bool(row and row["complete"])


def set_month_complete(conn: sqlite3.Connection, month: str, updated_utc: str) -> None:
    conn.execute(
        """
        INSERT INTO cli_month_progress (month, complete, updated_utc)
        VALUES (?, 1, ?)
        ON CONFLICT(month) DO UPDATE SET complete = 1, updated_utc = excluded.updated_utc
        """,
        (month, updated_utc),
    )
    conn.commit()


def asos_month_complete(conn: sqlite3.Connection, station: str, month: str) -> bool:
    row = conn.execute(
        "SELECT complete FROM asos_month_progress WHERE station = ? AND month = ?",
        (station, month),
    ).fetchone()
    return bool(row and row["complete"])


def set_asos_month_complete(
    conn: sqlite3.Connection,
    station: str,
    month: str,
    updated_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO asos_month_progress (station, month, complete, updated_utc)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(station, month) DO UPDATE SET
            complete = 1,
            updated_utc = excluded.updated_utc
        """,
        (station, month, updated_utc),
    )
    conn.commit()
