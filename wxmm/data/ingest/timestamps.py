"""Source publication timestamps. Never ingestion wall-clock.

Allowed to assume
    Kalshi candles: venue emission timestamp. NBM: per-cycle **measured**
    publication time (sample p90 441 / max 453 min; never a global 60-min
    constant). CLINYC: issuance timestamp from the AFOS product itself.

Must never
    Default ``available_at`` to ingest time. Use a 60-minute NBM assumption.
    Treat Weather Underground availability as known.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from wxmm.core.errors import SourceTimestampRequired
from wxmm.core.types import AsOfRecord, published_record
from wxmm.core.utc import require_utc

VOID_NBM_ASSUMED_LATENCY_MIN = 60
AVAILABILITY_UNKNOWN = "unknown"
AVAILABILITY_KNOWN = "known"


def require_source_published_at(
    source_published_at: datetime | None,
    *,
    source: str,
    field: str,
) -> datetime:
    """Fail if the source did not provide a publication timestamp."""
    if source_published_at is None:
        raise SourceTimestampRequired(
            f"{source} ingest cannot determine available_at from {field}; "
            "refusing to default to ingestion wall-clock"
        )
    return require_utc(source_published_at)


def kalshi_candle_available_at(emission_ts: datetime | None) -> datetime:
    """Kalshi candles: venue emission timestamp (``end_period_ts`` / equivalent)."""
    return require_source_published_at(
        emission_ts, source="kalshi_candle", field="emission_ts"
    )


def clinyc_available_at(issuance_ts_from_product: datetime | None) -> datetime:
    """CLINYC: issuance timestamp printed on the AFOS product, not fetch time."""
    return require_source_published_at(
        issuance_ts_from_product, source="clinyc", field="issuance_ts_from_product"
    )


@dataclass(frozen=True, slots=True)
class NbmCyclePublication:
    """Per-cycle measured publication. Do not substitute a global constant."""

    cycle_nominal_utc: datetime
    measured_published_at: datetime
    latency_min: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cycle_nominal_utc", require_utc(self.cycle_nominal_utc))
        object.__setattr__(
            self, "measured_published_at", require_utc(self.measured_published_at)
        )


def nbm_cycle_publication(
    *,
    cycle_nominal_utc: datetime,
    measured_published_at: datetime | None,
) -> NbmCyclePublication:
    published = require_source_published_at(
        measured_published_at,
        source="nbm",
        field="measured_published_at (per-cycle; 60 min assumption is void)",
    )
    nominal = require_utc(cycle_nominal_utc)
    latency_min = int((published - nominal).total_seconds() // 60)
    return NbmCyclePublication(
        cycle_nominal_utc=nominal,
        measured_published_at=published,
        latency_min=latency_min,
    )


def nbm_available_at(publication: NbmCyclePublication) -> datetime:
    return publication.measured_published_at


def wunderground_record(
    *,
    key: str,
    payload: object,
    valid_at: datetime,
    ingest_run_id: str,
) -> AsOfRecord:
    """WU update semantics are (verify). Mark AVAILABILITY_UNKNOWN.

    The leakage guard treats unknown availability as not yet available at
    any as_of — never as 'assume it was published'.
    """
    return published_record(
        key=key,
        payload=payload,
        valid_at=valid_at,
        source_published_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        source="wunderground",
        ingest_run_id=ingest_run_id,
        availability=AVAILABILITY_UNKNOWN,
    )


def kalshi_candle_record(
    *,
    key: str,
    payload: object,
    valid_at: datetime,
    emission_ts: datetime | None,
    ingest_run_id: str,
) -> AsOfRecord:
    available = kalshi_candle_available_at(emission_ts)
    return published_record(
        key=key,
        payload=payload,
        valid_at=valid_at,
        source_published_at=available,
        source="kalshi_candle",
        ingest_run_id=ingest_run_id,
        availability=AVAILABILITY_KNOWN,
    )


def clinyc_record(
    *,
    key: str,
    payload: object,
    valid_at: datetime,
    issuance_ts_from_product: datetime | None,
    ingest_run_id: str,
) -> AsOfRecord:
    available = clinyc_available_at(issuance_ts_from_product)
    return published_record(
        key=key,
        payload=payload,
        valid_at=valid_at,
        source_published_at=available,
        source="clinyc_afos",
        ingest_run_id=ingest_run_id,
        availability=AVAILABILITY_KNOWN,
    )


def nbm_cycle_record(
    *,
    key: str,
    payload: object,
    valid_at: datetime,
    publication: NbmCyclePublication,
    ingest_run_id: str,
) -> AsOfRecord:
    body = dict(payload) if isinstance(payload, dict) else {"value": payload}
    body["cycle_latency_min"] = publication.latency_min
    body["cycle_nominal_utc"] = publication.cycle_nominal_utc.isoformat()
    return published_record(
        key=key,
        payload=body,
        valid_at=valid_at,
        source_published_at=nbm_available_at(publication),
        source="nbm_idx",
        ingest_run_id=ingest_run_id,
        availability=AVAILABILITY_KNOWN,
    )
