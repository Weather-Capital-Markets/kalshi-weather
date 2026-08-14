"""Parse IEM ASOS CSV payloads into observation rows."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ingestion.climate_time import AsosObservation


def parse_asos_csv(text: str) -> list[AsosObservation]:
    """Parse an IEM asos.py comma-separated response body."""
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []
    observations: list[AsosObservation] = []
    for row in reader:
        valid_raw = (row.get("valid") or "").strip()
        tmpf_raw = (row.get("tmpf") or "").strip()
        if not valid_raw or not tmpf_raw or tmpf_raw in {"M", "null"}:
            continue
        try:
            tmpf = float(tmpf_raw)
        except ValueError:
            continue
        # IEM uses 'YYYY-MM-DD HH:MM' in UTC when tz=UTC is requested.
        valid = datetime.strptime(valid_raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        observations.append(AsosObservation(valid_utc=valid, tmpf=tmpf))
    return observations


def load_asos_observations_from_raw(raw_dir) -> list[AsosObservation]:
    from pathlib import Path

    from ingestion.writer import read_jsonl_gz

    root = Path(raw_dir)
    observations: list[AsosObservation] = []
    for path in sorted(root.glob("*/asos_obs/*.jsonl.gz")):
        for record in read_jsonl_gz(path):
            payload = record.get("payload") or {}
            text = payload.get("text") if isinstance(payload, dict) else None
            if isinstance(text, str):
                observations.extend(parse_asos_csv(text))
    observations.sort(key=lambda obs: obs.valid_utc)
    return observations
