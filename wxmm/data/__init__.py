"""As-of data: vintage store, coverage, ingest wrappers.

Allowed to assume
    Records carry ``valid_at``, ``available_at``, ``source``, ``ingest_run_id``.
    NBM lives at ``s3://noaa-nbm-grib2-pds`` with byte-range ``.idx`` access;
    publication latency is measured (p90 441 min, max 453 min), never the
    void 60-minute assumption. 18-h max window 12Z–06Z is not the CLI climate
    day. 99-level window through 2026-05-03; 21-level from 2026-05-04/05.
    Existing ``ingestion/nbm_*`` owns grib decode.

Must never
    Offer a 'latest' read. Download whole grib2 files. Use pandas. Assume
    NBM window equals the climate day. Drop missing books or zero-volume
    days from coverage. Call wall-clock.
"""

from __future__ import annotations
