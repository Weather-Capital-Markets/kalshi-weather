"""Durable as-of store and content-addressed snapshots.

Writes record ``valid_at``, ``available_at``, ``source``, ``ingest_run_id``.
Reads take ``as_of``. Snapshots are immutable; ``snapshot_id`` is the sha256
of the canonical record set and must appear on every backtest result.

NBM facts here are plumbing constants, not a model. Byte-range via ``.idx``;
never whole files. Thin-wrap ``ingestion.nbm_*`` rather than rewriting decode.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from wxmm.core.types import AsOfRecord, InMemoryAsOfStore
from wxmm.core.utc import require_utc

NBM_BUCKET = "s3://noaa-nbm-grib2-pds"
NBM_PUBLICATION_P90_MIN = 441  # measured; 60 min was wrong
NBM_PUBLICATION_MAX_MIN = 453
NBM_MAX_WINDOW = "12Z-06Z"  # ≠ CLI climate day
NBM_99_LEVEL_THROUGH = date(2026, 5, 3)
NBM_HOURLY_21_FROM = date(2026, 5, 4)
NBM_WINDOW_21_FROM = date(2026, 5, 5)


def nbm_window_percentile_count(cycle_date: date) -> int:
    """99-level window through 2026-05-03; 21-level from 2026-05-05."""
    if cycle_date <= NBM_99_LEVEL_THROUGH:
        return 99
    if cycle_date >= NBM_WINDOW_21_FROM:
        return 21
    return 99  # 2026-05-04: hourly 21-level onset; window still 99


def nbm_hourly_percentile_count(cycle_date: date) -> int | None:
    if cycle_date < NBM_HOURLY_21_FROM:
        return None
    return 21


def _record_tuple(record: AsOfRecord) -> tuple[str, str, str, str, str, str]:
    payload = json.dumps(record.payload, sort_keys=True, default=str, separators=(",", ":"))
    return (
        record.key,
        record.valid_at.isoformat(),
        record.available_at.isoformat(),
        record.source,
        record.ingest_run_id,
        payload,
    )


def snapshot_id_for(records: Iterable[AsOfRecord]) -> str:
    digest = hashlib.sha256()
    for item in sorted(_record_tuple(record) for record in records):
        digest.update(("|".join(item) + "\n").encode())
    return digest.hexdigest()


@dataclass
class Snapshot:
    snapshot_id: str
    records: tuple[AsOfRecord, ...]
    created_as_of: datetime | None = None


class VintageStore:
    """As-of store with an immutable snapshot() of current contents."""

    def __init__(self) -> None:
        self._inner = InMemoryAsOfStore()
        self._all: list[AsOfRecord] = []

    def write(
        self,
        key: str,
        payload: object,
        *,
        valid_at: datetime,
        available_at: datetime,
        source: str,
        ingest_run_id: str,
    ) -> AsOfRecord:
        record = AsOfRecord(
            key=key,
            payload=payload,
            valid_at=valid_at,
            available_at=available_at,
            source=source,
            ingest_run_id=ingest_run_id,
        )
        self._inner.put(record)
        self._all.append(record)
        return record

    def get(self, key: str, as_of: datetime) -> AsOfRecord:
        return self._inner.get(key, require_utc(as_of))

    def snapshot(self) -> Snapshot:
        frozen = tuple(self._all)
        return Snapshot(snapshot_id=snapshot_id_for(frozen), records=frozen)

    def persist_parquet(self, path: Path) -> None:
        """Write records to parquet (polars) so duckdb can scan them."""
        import polars as pl

        path = Path(path)
        rows = [
            {
                "key": rec.key,
                "payload_json": json.dumps(rec.payload, default=str),
                "valid_at": rec.valid_at.isoformat(),
                "available_at": rec.available_at.isoformat(),
                "source": rec.source,
                "ingest_run_id": rec.ingest_run_id,
            }
            for rec in self._all
        ]
        schema = {
            "key": pl.String,
            "payload_json": pl.String,
            "valid_at": pl.String,
            "available_at": pl.String,
            "source": pl.String,
            "ingest_run_id": pl.String,
        }
        frame = pl.DataFrame(rows, schema=schema)
        if path.suffix == ".parquet":
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.write_parquet(path)
        else:
            path.mkdir(parents=True, exist_ok=True)
            frame.write_parquet(path / "vintage.parquet")
