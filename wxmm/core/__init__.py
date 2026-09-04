"""Core types, money, the single time authority, and underlyings.

Allowed to assume
    All internal timestamps are UTC-aware. Station identifiers ``KNYC`` and
    ``KLGA`` share Eastern local-standard-time (UTC−5) for climate days.

Must never
    Call ``datetime.now`` / ``time.time``. Convert to local or climate days
    outside ``timeauth``. Treat missing numeric fields as zero. Use float for
    cash. Net Kalshi and Polymarket positions. Silently drop unreadably-future
    records (raise ``LeakageError``).
"""

from __future__ import annotations

from wxmm.core.errors import (
    ConfigNotPreregistered,
    CoverageReportMissing,
    EmptyPipelineError,
    HeldOutLockedError,
    LeakageError,
    LimitBreach,
    MissingDataError,
    NonFungibleNettingError,
    ResidualBasisRejected,
    SizeUnknownFlag,
    SourceTimestampRequired,
    UnverifiedFactError,
    UnverifiedFeeSchedule,
    UnverifiedSettlementFee,
    WallClockError,
    WxmmError,
)
from wxmm.core.money import Money
from wxmm.core.types import (
    AsOfRecord,
    ClockBoundStore,
    FactStatus,
    FrozenClock,
    InMemoryAsOfStore,
    VenueFact,
)
from wxmm.core.underlying import Underlying, UnderlyingRegistry
from wxmm.core.utc import require_utc

__all__ = [
    "AsOfRecord",
    "ClockBoundStore",
    "ConfigNotPreregistered",
    "CoverageReportMissing",
    "EmptyPipelineError",
    "FactStatus",
    "FrozenClock",
    "HeldOutLockedError",
    "InMemoryAsOfStore",
    "LeakageError",
    "LimitBreach",
    "MissingDataError",
    "Money",
    "NonFungibleNettingError",
    "ResidualBasisRejected",
    "SizeUnknownFlag",
    "SourceTimestampRequired",
    "Underlying",
    "UnderlyingRegistry",
    "UnverifiedFactError",
    "UnverifiedFeeSchedule",
    "UnverifiedSettlementFee",
    "VenueFact",
    "WallClockError",
    "WxmmError",
    "require_utc",
]
