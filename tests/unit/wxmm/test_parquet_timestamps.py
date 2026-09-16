"""Sub-second parquet round-trips under the pinned pyarrow.

``requirements-analysis.txt`` pinned pyarrow 17.0.0 in Session 6a with cfgrib;
no comment explained 17 specifically. CI now installs the wxmm pin 25.0.1.
Whole-second fixtures would not catch a us→ms coercion; the WS feed supplies
``ts_ms``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tests.unit.wxmm.test_c1_x1 import _trade
from wxmm.analysis.trades_ingest import read_trades_parquet, trades_to_parquet
from wxmm.data.vintage import VintageStore

UTC = timezone.utc
# 348599 µs is not a whole millisecond. ts_ms 1_726_372_651_348 is.
TS_US = datetime(2026, 9, 15, 3, 17, 31, 348599, tzinfo=UTC)
TS_MS_UNIX = 1_726_372_651_348


def test_pinned_pyarrow_is_25_0_1() -> None:
    assert version("pyarrow") == "25.0.1"
    assert pa.__version__ == "25.0.1"


def test_trade_parquet_roundtrip_keeps_subsecond_created_time(tmp_path: Path) -> None:
    trade = _trade(created=TS_US.isoformat())
    dest = tmp_path / "trades.parquet"
    trades_to_parquet([trade], dest)
    recovered = read_trades_parquet(dest)
    assert len(recovered) == 1
    got = recovered[0].created_time.isoformat().encode("utf-8")
    want = TS_US.isoformat().encode("utf-8")
    assert got == want
    assert recovered[0].created_time == TS_US
    assert recovered[0].created_time.microsecond == 348599


def test_pyarrow_timestamp_us_roundtrip_keeps_ts_ms_int64(tmp_path: Path) -> None:
    ts_ms = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=TS_MS_UNIX)
    assert ts_ms.microsecond == 348_000
    table = pa.table(
        {
            "valid_at": pa.array([ts_ms], type=pa.timestamp("us", tz="UTC")),
            "available_at": pa.array([ts_ms], type=pa.timestamp("us", tz="UTC")),
        }
    )
    dest = tmp_path / "clock.parquet"
    pq.write_table(table, dest)
    got = pq.read_table(dest)
    want_us = int(ts_ms.timestamp() * 1_000_000)
    for name in ("valid_at", "available_at"):
        col = got.column(name)
        assert col.type == pa.timestamp("us", tz="UTC")
        raw = col.cast(pa.int64()).to_pylist()[0]
        assert raw == want_us
        recovered = col.to_pylist()[0]
        assert recovered.isoformat().encode("utf-8") == ts_ms.isoformat().encode("utf-8")


def test_vintage_parquet_keeps_subsecond_valid_and_available_at(tmp_path: Path) -> None:
    store = VintageStore()
    store.write(
        "k",
        {"x": 1},
        valid_at=TS_US,
        available_at=TS_US,
        source="test",
        ingest_run_id="run-1",
    )
    dest = tmp_path / "vintage.parquet"
    store.persist_parquet(dest)
    import polars as pl

    frame = pl.read_parquet(dest)
    want = TS_US.isoformat().encode("utf-8")
    assert str(frame["valid_at"][0]).encode("utf-8") == want
    assert str(frame["available_at"][0]).encode("utf-8") == want
