"""Ingest adapters. Thin wrap existing ``ingestion/nbm_*``; no grib rewrite.

Allowed to assume
    Byte-range via ``.idx``. Vintage constants live in ``wxmm.data.vintage``.

Must never
    Download whole grib2 files. Assume 60-minute NBM latency. Treat the
    12Z–06Z max window as a climate day.
"""

from __future__ import annotations

from wxmm.data.vintage import (
    NBM_BUCKET,
    NBM_MAX_WINDOW,
    NBM_PUBLICATION_MAX_MIN,
    NBM_PUBLICATION_P90_MIN,
)


def nbm_archive_pointer() -> dict[str, str | int]:
    """Where NBM lives. Decode remains in ``ingestion.nbm_archive``."""
    return {
        "bucket": NBM_BUCKET,
        "max_window": NBM_MAX_WINDOW,
        "publication_p90_min": NBM_PUBLICATION_P90_MIN,
        "publication_max_min": NBM_PUBLICATION_MAX_MIN,
        "decode_module": "ingestion.nbm_archive",
        "access": "idx_byte_range_never_whole_file",
    }
