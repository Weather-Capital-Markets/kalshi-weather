"""Parse IEM ASOS CSV payloads into observation rows."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

from ingestion.climate_time import AsosObservation

_STATION_KEY_RE = re.compile(r"^([A-Z0-9]+)_(\d{4}-\d{2})$")


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


def infer_station_from_key(key: str) -> str | None:
    match = _STATION_KEY_RE.match(key)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{4}-\d{2}", key):
        return "NYC"
    return None


def load_asos_observations_from_raw(
    raw_dir,
    *,
    station: str = "NYC",
) -> list[AsosObservation]:
    from ingestion.writer import read_jsonl_gz

    root = Path(raw_dir)
    observations: list[AsosObservation] = []
    for path in sorted(root.glob("*/asos_obs/*.jsonl.gz")):
        file_station = infer_station_from_key(path.stem)
        for record in read_jsonl_gz(path):
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            payload_station = payload.get("station")
            if isinstance(payload_station, str) and payload_station.strip():
                record_station = payload_station.strip()
            elif file_station is not None:
                record_station = file_station
            else:
                record_station = "NYC"
            if record_station != station:
                continue
            text = payload.get("text")
            if isinstance(text, str):
                observations.extend(parse_asos_csv(text))
    observations.sort(key=lambda obs: obs.valid_utc)
    return observations
