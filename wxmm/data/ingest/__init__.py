"""Ingest adapters. Thin wrap existing ``ingestion/nbm_*``; no grib rewrite.

Allowed to assume
    Byte-range via ``.idx``. ``available_at`` comes from source publication
    metadata (see ``timestamps``). NBM sample p90 441 / max 453 min is a
    measured distribution, not a per-record default.

Must never
    Download whole grib2 files. Assume 60-minute NBM latency. Treat the
    12Z–06Z max window as a climate day. Default ``available_at`` to ingest
    wall-clock.
"""

from __future__ import annotations

from wxmm.data.ingest.timestamps import (
    AVAILABILITY_UNKNOWN,
    VOID_NBM_ASSUMED_LATENCY_MIN,
    clinyc_record,
    kalshi_candle_record,
    nbm_cycle_publication,
    nbm_cycle_record,
    wunderground_record,
)
from wxmm.data.vintage import (
    NBM_BUCKET,
    NBM_MAX_WINDOW,
    NBM_PUBLICATION_MAX_MIN,
    NBM_PUBLICATION_P90_MIN,
)

__all__ = [
    "AVAILABILITY_UNKNOWN",
    "VOID_NBM_ASSUMED_LATENCY_MIN",
    "clinyc_record",
    "kalshi_candle_record",
    "nbm_archive_pointer",
    "nbm_cycle_publication",
    "nbm_cycle_record",
    "wunderground_record",
]


def nbm_archive_pointer() -> dict[str, str | int]:
    """Where NBM lives. Decode remains in ``ingestion.nbm_archive``."""
    return {
        "bucket": NBM_BUCKET,
        "max_window": NBM_MAX_WINDOW,
        "publication_p90_min_sample": NBM_PUBLICATION_P90_MIN,
        "publication_max_min_sample": NBM_PUBLICATION_MAX_MIN,
        "void_assumed_latency_min": VOID_NBM_ASSUMED_LATENCY_MIN,
        "decode_module": "ingestion.nbm_archive",
        "access": "idx_byte_range_never_whole_file",
        "available_at": "per_cycle_measured_publication_never_global_constant",
    }
