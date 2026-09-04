"""available_at is source publication time, never ingest wall-clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.core.errors import LeakageError, SourceTimestampRequired
from wxmm.core.types import FrozenClock, InMemoryAsOfStore
from wxmm.data.ingest import nbm_archive_pointer
from wxmm.data.ingest.timestamps import (
    VOID_NBM_ASSUMED_LATENCY_MIN,
    clinyc_record,
    kalshi_candle_record,
    nbm_cycle_publication,
    nbm_cycle_record,
    wunderground_record,
)

UTC = timezone.utc
TS = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def test_missing_source_timestamp_fails() -> None:
    with pytest.raises(SourceTimestampRequired, match="kalshi_candle"):
        kalshi_candle_record(
            key="c",
            payload={},
            valid_at=TS,
            emission_ts=None,
            ingest_run_id="run",
        )
    with pytest.raises(SourceTimestampRequired, match="clinyc"):
        clinyc_record(
            key="cli",
            payload={},
            valid_at=TS,
            issuance_ts_from_product=None,
            ingest_run_id="run",
        )
    with pytest.raises(SourceTimestampRequired, match="nbm"):
        nbm_cycle_publication(cycle_nominal_utc=TS, measured_published_at=None)


def test_wunderground_unknown_is_unreadable_at_any_as_of() -> None:
    rec = wunderground_record(
        key="wu:KLGA:2026-07-04",
        payload={"high_f": 88},
        valid_at=TS,
        ingest_run_id="run",
    )
    assert rec.availability == "unknown"
    store = InMemoryAsOfStore()
    store.put(rec)
    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    with pytest.raises(LeakageError, match="AVAILABILITY_UNKNOWN"):
        store.get(rec.key, as_of=far_future)
    clock = FrozenClock(far_future)
    with pytest.raises(LeakageError, match="AVAILABILITY_UNKNOWN"):
        store.get_record(rec, as_of=clock.now())


def test_nbm_does_not_use_sixty_minute_assumption() -> None:
    assert VOID_NBM_ASSUMED_LATENCY_MIN == 60
    pointer = nbm_archive_pointer()
    assert pointer["void_assumed_latency_min"] == 60
    assert pointer["publication_p90_min_sample"] == 441
    assert "per_cycle_measured" in str(pointer["available_at"])
    published = TS + timedelta(minutes=441)
    pub = nbm_cycle_publication(cycle_nominal_utc=TS, measured_published_at=published)
    rec = nbm_cycle_record(
        key="nbm:2026-07-04T12",
        payload={"cycle": "12Z"},
        valid_at=TS,
        publication=pub,
        ingest_run_id="run",
    )
    assert rec.available_at == published
    assert rec.available_at != TS + timedelta(minutes=VOID_NBM_ASSUMED_LATENCY_MIN)
    assert rec.payload["cycle_latency_min"] == 441


def test_kalshi_candle_uses_emission_timestamp() -> None:
    emission = TS + timedelta(minutes=5)
    rec = kalshi_candle_record(
        key="candle",
        payload={},
        valid_at=TS,
        emission_ts=emission,
        ingest_run_id="run",
    )
    assert rec.available_at == emission
    ingest_wall = datetime(2026, 7, 4, 18, 0, tzinfo=UTC)
    assert rec.available_at != ingest_wall
