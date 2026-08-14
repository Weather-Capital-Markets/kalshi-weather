"""Append-only raw JSONL.gz writer with UTC daily rotation."""

from __future__ import annotations

import gzip
import io
import json
import os
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_date_str(ts_utc: str | None = None) -> str:
    if ts_utc:
        return ts_utc[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RawJsonlWriter:
    """Write capture envelopes to daily rotated gzip JSONL files.

    Each line is stored as an independent gzip member so same-day restarts
    append safely and readers can iterate all members.
    """

    def __init__(self, raw_dir: str | Path) -> None:
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._checked_paths: set[Path] = set()

    def write(
        self,
        *,
        ts_utc: str,
        endpoint: str,
        category: str,
        key: str,
        http_status: int | None,
        latency_ms: int,
        payload: Any,
    ) -> Path:
        date_part = utc_date_str(ts_utc)
        path = self._file_path(date_part, category, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path not in self._checked_paths:
            self._repair_truncated_tail(path)
            self._checked_paths.add(path)
        record = {
            "ts_utc": ts_utc,
            "endpoint": endpoint,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "payload": payload,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        payload_bytes = line.encode("utf-8")
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(payload_bytes)
        with path.open("ab") as handle:
            handle.write(buf.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def close(self) -> None:
        return None

    def _file_path(self, date_part: str, category: str, key: str) -> Path:
        safe_key = key.replace("/", "_")
        return self.raw_dir / date_part / category / f"{safe_key}.jsonl.gz"

    @staticmethod
    def _repair_truncated_tail(path: Path) -> None:
        """Drop only an incomplete final gzip member left by a hard crash."""
        if not path.exists() or path.stat().st_size == 0:
            return
        data = path.read_bytes()
        offset = 0
        last_complete = 0
        while offset < len(data):
            decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            try:
                decompressor.decompress(data[offset:])
                decompressor.flush()
            except zlib.error:
                break
            if not decompressor.eof:
                break
            consumed = len(data) - offset - len(decompressor.unused_data)
            if consumed <= 0:
                break
            offset += consumed
            last_complete = offset
        if last_complete == len(data):
            return
        with path.open("r+b") as handle:
            handle.truncate(last_complete)
            handle.flush()
            os.fsync(handle.fileno())


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    """Read complete JSON lines from a gzip JSONL file (test helper)."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        while True:
            start = handle.tell()
            try:
                with gzip.GzipFile(fileobj=handle, mode="rb") as gz:
                    for raw_line in gz:
                        line = raw_line.decode("utf-8").strip()
                        if line:
                            records.append(json.loads(line))
            except EOFError:
                break
            if handle.tell() == start:
                break
    return records
