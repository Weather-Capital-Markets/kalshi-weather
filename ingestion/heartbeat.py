"""SQLite heartbeat logging for every poll attempt."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class HeartbeatRow:
    hour_utc: str
    endpoint: str
    attempts: int
    failures: int


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS poll_attempts (
  ts_utc TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  ticker TEXT NOT NULL,
  ok INTEGER NOT NULL,
  http_status INTEGER,
  latency_ms INTEGER,
  error_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_attempts_ts ON poll_attempts(ts_utc);
CREATE VIEW IF NOT EXISTS heartbeat_hourly AS
SELECT
  substr(ts_utc, 1, 13) || ':00:00Z' AS hour_utc,
  endpoint,
  COUNT(*) AS attempts,
  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures
FROM poll_attempts
GROUP BY hour_utc, endpoint;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def record_attempt(
    conn: sqlite3.Connection,
    *,
    ts_utc: str,
    endpoint: str,
    ticker: str,
    ok: bool,
    http_status: int | None,
    latency_ms: int,
    error_text: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO poll_attempts (
            ts_utc, endpoint, ticker, ok, http_status, latency_ms, error_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_utc,
            endpoint,
            ticker,
            1 if ok else 0,
            http_status,
            latency_ms,
            error_text,
        ),
    )
    conn.commit()


def last_hour_summary(conn: sqlite3.Connection) -> list[HeartbeatRow]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3
    ] + "Z"
    rows = conn.execute(
        """
        SELECT
            substr(ts_utc, 1, 13) || ':00:00Z' AS hour_utc,
            endpoint,
            COUNT(*) AS attempts,
            SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures
        FROM poll_attempts
        WHERE ts_utc >= ?
        GROUP BY hour_utc, endpoint
        ORDER BY hour_utc, endpoint
        """,
        (cutoff,),
    ).fetchall()
    return [
        HeartbeatRow(
            hour_utc=row["hour_utc"],
            endpoint=row["endpoint"],
            attempts=row["attempts"],
            failures=row["failures"],
        )
        for row in rows
    ]


def format_status_summary(rows: list[HeartbeatRow]) -> str:
    if not rows:
        return "No poll attempts in the last hour."
    lines = ["Last-hour heartbeat summary:", "hour_utc | endpoint | attempts | failures"]
    for row in rows:
        lines.append(f"{row.hour_utc} | {row.endpoint} | {row.attempts} | {row.failures}")
    return "\n".join(lines)
