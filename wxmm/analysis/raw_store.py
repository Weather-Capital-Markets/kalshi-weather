"""Content-addressed HTTP bodies and per-ticker pagination checkpoints.

Allowed to assume
    The caller writes the blob *before* parsing into parquet. A process that
    dies mid-page can resume from the last durable cursor.

Must never
    Store API keys. Rewrite a blob under a different hash. Drop a ticker
    because one page failed the complement check.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

SourceEndpoint = Literal["historical", "live"]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def ticker_filename(ticker: str) -> str:
    return ticker.replace("/", "_")


@dataclass(frozen=True, slots=True)
class StoredBody:
    url: str
    body_sha256: str
    nbytes: int
    path: Path


class ContentAddressedRawStore:
    """Store response bodies by SHA-256; index URL + hash before parquet."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.blobs = root / "blobs"
        self.index_path = root / "index.jsonl"
        self.blobs.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        url: str,
        body: bytes,
        *,
        meta: dict[str, Any] | None = None,
    ) -> StoredBody:
        digest = hashlib.sha256(body).hexdigest()
        dest = self.blobs / digest[:2] / f"{digest}.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(body)
        record: dict[str, Any] = {
            "url": url,
            "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "body_sha256": digest,
            "nbytes": len(body),
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        if meta:
            record.update(meta)
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return StoredBody(url=url, body_sha256=digest, nbytes=len(body), path=dest)


@dataclass
class EndpointCursor:
    path: str
    cursor: str | None = None
    last_created_time: str | None = None
    n_trades: int = 0
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "cursor": self.cursor,
            "last_created_time": self.last_created_time,
            "n_trades": self.n_trades,
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, default_path: str) -> EndpointCursor:
        return cls(
            path=str(raw.get("path") or default_path),
            cursor=raw.get("cursor") or None,
            last_created_time=raw.get("last_created_time") or None,
            n_trades=int(raw.get("n_trades") or 0),
            done=bool(raw.get("done")),
        )


@dataclass
class TickerCheckpoint:
    ticker: str
    historical: EndpointCursor
    live: EndpointCursor

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "historical": self.historical.to_dict(),
            "live": self.live.to_dict(),
        }

    @classmethod
    def fresh(cls, ticker: str) -> TickerCheckpoint:
        return cls(
            ticker=ticker,
            historical=EndpointCursor(path="/historical/trades"),
            live=EndpointCursor(path="/markets/trades"),
        )

    @property
    def complete(self) -> bool:
        return self.historical.done and self.live.done


class CheckpointStore:
    """One JSON file per ticker: cursor + last created_time after every page."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.done_path = root / "done.jsonl"
        self._done_lock = threading.Lock()

    def path_for(self, ticker: str) -> Path:
        return self.root / f"{ticker_filename(ticker)}.json"

    def inflight_path(self, ticker: str, endpoint: SourceEndpoint) -> Path:
        return self.root / f"{ticker_filename(ticker)}.{endpoint}.jsonl"

    def ticker_parquet(self, ticker_dir: Path, ticker: str) -> Path:
        ticker_dir.mkdir(parents=True, exist_ok=True)
        return ticker_dir / f"{ticker_filename(ticker)}.parquet"

    def load(self, ticker: str) -> TickerCheckpoint | None:
        path = self.path_for(ticker)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return TickerCheckpoint(
            ticker=str(raw.get("ticker") or ticker),
            historical=EndpointCursor.from_dict(
                raw.get("historical") or {}, default_path="/historical/trades"
            ),
            live=EndpointCursor.from_dict(raw.get("live") or {}, default_path="/markets/trades"),
        )

    def save(self, checkpoint: TickerCheckpoint) -> None:
        atomic_write_json(self.path_for(checkpoint.ticker), checkpoint.to_dict())

    def mark_done(self, ticker: str) -> None:
        with self._done_lock:
            with self.done_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ticker": ticker, "done": True}) + "\n")

    def done_tickers(self) -> set[str]:
        found: set[str] = set()
        if not self.done_path.exists():
            return found
        for line in self.done_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("done"):
                found.add(str(row["ticker"]))
        return found
