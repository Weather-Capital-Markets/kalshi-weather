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
    CredentialsUnavailable,
    EmptyPipelineError,
    FeeMismatch,
    HeldOutLockedError,
    InvalidTransition,
    LeakageError,
    LimitBreach,
    MissingDataError,
    NonFungibleNettingError,
    RateLimited,
    ResidualBasisRejected,
    SendTokenError,
    SizeUnknownFlag,
    SourceTimestampRequired,
    StaleBookError,
    UnreconciledBreak,
    UnverifiedFactError,
    UnverifiedFeeSchedule,
    UnverifiedSettlementFee,
    WallClockError,
    WxmmError,
)
from wxmm.core.money import Money
from wxmm.core.types import (
    AsOfRecord,
    BookSource,
    ClockBoundStore,
    FactStatus,
    FrozenClock,
    InMemoryAsOfStore,
    VenueFact,
    clip_extreme_touch,
    published_record,
    received_record,
    require_clock_bound_book_source,
    require_clock_bound_store,
)
from wxmm.core.underlying import Underlying, UnderlyingRegistry
from wxmm.core.utc import require_utc

__all__ = [
    "AsOfRecord",
    "BookSource",
    "ClockBoundStore",
    "ConfigNotPreregistered",
    "CoverageReportMissing",
    "CredentialsUnavailable",
    "EmptyPipelineError",
    "FactStatus",
    "FeeMismatch",
    "FrozenClock",
    "HeldOutLockedError",
    "InMemoryAsOfStore",
    "InvalidTransition",
    "LeakageError",
    "LimitBreach",
    "MissingDataError",
    "Money",
    "NonFungibleNettingError",
    "RateLimited",
    "ResidualBasisRejected",
    "SendTokenError",
    "SizeUnknownFlag",
    "SourceTimestampRequired",
    "StaleBookError",
    "Underlying",
    "UnderlyingRegistry",
    "UnreconciledBreak",
    "UnverifiedFactError",
    "UnverifiedFeeSchedule",
    "UnverifiedSettlementFee",
    "VenueFact",
    "WallClockError",
    "WxmmError",
    "published_record",
    "received_record",
    "require_clock_bound_book_source",
    "require_clock_bound_store",
    "require_utc",
    "clip_extreme_touch",
]
