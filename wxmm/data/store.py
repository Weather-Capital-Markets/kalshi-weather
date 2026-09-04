"""Store facade. DuckDB + parquet; no ORM; no pandas."""

from __future__ import annotations

from pathlib import Path

from wxmm.data.vintage import VintageStore


def open_vintage(_path: Path | None = None) -> VintageStore:
    """Return a vintage store. Persistence is opt-in via ``persist_parquet``."""
    return VintageStore()
