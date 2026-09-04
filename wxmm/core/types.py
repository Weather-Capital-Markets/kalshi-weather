"""Domain records, as-of store, clock protocol, and venue-fact flags.

Allowed to assume
    Every durable record has ``valid_at`` and ``available_at``. Reads are
    ``get(key, as_of)``. Missing is not zero.

Must never
    Expose a \"latest\" API. Return a record with ``available_at > as_of``.
    Call ``datetime.now`` / ``time.time``. Convert timezones (use ``timeauth``).
    Treat ``None`` volume as ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from wxmm.core.errors import LeakageError, MissingDataError, UnverifiedFactError
from wxmm.core.utc import UTC, require_utc


class FactStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    TAGGED_ASSUMPTION = "tagged_assumption"
    HYPOTHESIS_UNDER_TEST = "hypothesis_under_test"


@dataclass(frozen=True, slots=True)
class VenueFact:
    """A load-bearing venue claim with an explicit verification state.

    Unverified or expired facts fail loudly when required for capital /
    executable claims. Comments are not a substitute for this type.
    """

    name: str
    status: FactStatus
    source: str
    verified_on: datetime | None = None
    reverify_after: datetime | None = None
    note: str = ""

    def require_for_capital(self, as_of: datetime) -> None:
        as_of = require_utc(as_of)
        if self.status != FactStatus.VERIFIED:
            raise UnverifiedFactError(
                f"{self.name} status={self.status.value} source={self.source!r} "
                f"is not verified; cannot use for capital / executable claims"
            )
        if self.reverify_after is not None and as_of > require_utc(self.reverify_after):
            raise UnverifiedFactError(
                f"{self.name} verified {self.verified_on}; "
                f"re-verify after {self.reverify_after.isoformat()} before capital "
                f"(as_of={as_of.isoformat()})"
            )


# Kalshi weather maker fee: $0.00 verified 2026-08-15. Re-verify before capital.
KALSHI_WEATHER_MAKER_FEE = VenueFact(
    name="kalshi_weather_maker_fee_kxhighny",
    status=FactStatus.VERIFIED,
    source="Kalshi live fee schedule page 2026-08-15 [V-LOCAL] venue-facts.md §2.1",
    verified_on=datetime(2026, 8, 15, tzinfo=UTC),
    reverify_after=datetime(2026, 8, 15, tzinfo=UTC),
    note="KXHIGHNY / KXHIGH* maker fee $0.00. Time-sensitive; re-check before capital.",
)

# Polymarket fee fields observed but base and units UNVERIFIED.
POLYMARKET_FEE_SCHEDULE = VenueFact(
    name="polymarket_nyc_fee_schedule",
    status=FactStatus.UNVERIFIED,
    source=(
        "CLOB feeSchedule probe 2026-08-18: rate 0.05 rebateRate 0.25 takerOnly "
        "(units UNVERIFIED)"
    ),
    note="Cost model must raise UnverifiedFeeSchedule until verified with date+source.",
)

# Emit-on-change candlesticks: hypothesis under test (0.9% forward match unresolved).
KALSHI_CANDLE_EMIT_ON_CHANGE = VenueFact(
    name="kalshi_candlestick_emit_on_change",
    status=FactStatus.HYPOTHESIS_UNDER_TEST,
    source="venue-facts.md §1.7; 0.9% forward match unresolved",
    note="Carry-forward reconstruction is a named parameterized policy with staleness.",
)

# Bracket boundary convention by era: UNVERIFIED — enumerator, do not hard-code widths.
KALSHI_BRACKET_BOUNDARY_CONVENTION = VenueFact(
    name="kalshi_bracket_boundary_convention",
    status=FactStatus.UNVERIFIED,
    source="stable 6-bracket regime from 2022-12-11; boundary convention by era UNVERIFIED",
    note="Enumerate observed brackets; do not hard-code widths.",
)


class CarryForwardPolicy(str, Enum):
    """Named parameterized book reconstruction. Staleness is always recorded."""

    NONE = "none"
    LAST_QUOTE = "last_quote"


@dataclass(frozen=True, slots=True)
class AsOfRecord:
    """Point-in-time record. ``valid_at`` is when the fact was true;
    ``available_at`` is the earliest as-of a backtest may read it."""

    key: str
    payload: object
    valid_at: datetime
    available_at: datetime
    source: str
    ingest_run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_at", require_utc(self.valid_at))
        object.__setattr__(self, "available_at", require_utc(self.available_at))


def assert_available(record: AsOfRecord, as_of: datetime, *, key: str | None = None) -> None:
    """Raise ``LeakageError`` if ``record`` is not readable at ``as_of``.

    Never warn. Never skip. Never return the record.
    """
    as_of_utc = require_utc(as_of)
    if record.available_at > as_of_utc:
        label = key if key is not None else record.key
        raise LeakageError(
            f"record {label!r} available_at={record.available_at.isoformat()} "
            f"> as_of={as_of_utc.isoformat()}"
        )


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Simulation or injected 'now'. Never wall-clock in backtest."""
        ...


@dataclass
class FrozenClock:
    """Deterministic clock. The only 'now' a strategy may see in tests."""

    _now: datetime

    def __post_init__(self) -> None:
        self._now = require_utc(self._now)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = require_utc(ts)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


@runtime_checkable
class AsOfStore(Protocol):
    def get(self, key: str, as_of: datetime) -> AsOfRecord:
        """Latest record with ``available_at <= as_of``. No 'latest' without as_of."""
        ...

    def put(self, record: AsOfRecord) -> None: ...


class InMemoryAsOfStore:
    """As-of store used by the leakage canary and unit tests.

    As-of ``get`` returns the newest record with ``available_at <= as_of``.
    Requesting a specific record that is not yet available raises
    ``LeakageError`` via ``get_record``. There is no ``latest()``.
    """

    def __init__(self) -> None:
        self._rows: dict[str, list[AsOfRecord]] = {}

    def put(self, record: AsOfRecord) -> None:
        self._rows.setdefault(record.key, []).append(record)

    def get(self, key: str, as_of: datetime) -> AsOfRecord:
        as_of_utc = require_utc(as_of)
        rows = self._rows.get(key, [])
        readable = [row for row in rows if row.available_at <= as_of_utc]
        if not readable:
            if rows:
                # Data exists but only in the future relative to as_of.
                # Honest as-of: missing at this clock. Cheat with a later
                # as_of is caught by ClockBoundStore before reaching here.
                raise MissingDataError(
                    f"key {key!r} has {len(rows)} record(s) but none with "
                    f"available_at <= {as_of_utc.isoformat()}"
                )
            raise MissingDataError(f"key {key!r} has no records")
        return max(readable, key=lambda row: (row.available_at, row.valid_at))

    def get_record(self, record: AsOfRecord, as_of: datetime) -> AsOfRecord:
        """Read this exact record, or ``LeakageError`` — never skip it."""
        assert_available(record, as_of)
        return record


class ClockBoundStore:
    """Store that refuses as_of after the bound clock. Backtest default."""

    def __init__(self, inner: AsOfStore, clock: Clock) -> None:
        self._inner = inner
        self._clock = clock

    def put(self, record: AsOfRecord) -> None:
        self._inner.put(record)

    def get(self, key: str, as_of: datetime) -> AsOfRecord:
        as_of_utc = require_utc(as_of)
        now = require_utc(self._clock.now())
        if as_of_utc > now:
            raise LeakageError(
                f"as_of={as_of_utc.isoformat()} is after clock.now()={now.isoformat()} "
                f"for key {key!r}"
            )
        return self._inner.get(key, as_of_utc)

    def get_record(self, record: AsOfRecord, as_of: datetime) -> AsOfRecord:
        as_of_utc = require_utc(as_of)
        now = require_utc(self._clock.now())
        if as_of_utc > now:
            raise LeakageError(
                f"as_of={as_of_utc.isoformat()} is after clock.now()={now.isoformat()} "
                f"for record {record.key!r}"
            )
        assert_available(record, as_of_utc)
        return record


@dataclass(frozen=True, slots=True)
class BookLevel:
    price_cents: int
    size: int | None  # None = SIZE_UNKNOWN; 0 is a real zero size


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """One book at one instant. Empty is not a 0/100 quote.

    Kalshi renders an empty book as bid 0.00 / ask 1.00 (venue-facts §1.4).
    Adapters must translate that to ``two_sided=False`` / empty levels, not
    to tradable prices.
    """

    market_id: str
    valid_at: datetime
    available_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    volume: int | None  # None = missing; 0 = zero volume (distinct coverage rows)
    ask_size_known: bool
    reconstructed: bool
    staleness: timedelta
    two_sided: bool
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_at", require_utc(self.valid_at))
        object.__setattr__(self, "available_at", require_utc(self.available_at))


@dataclass(frozen=True, slots=True)
class Trade:
    market_id: str
    ts: datetime
    available_at: datetime
    price_cents: int
    size: int
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", require_utc(self.ts))
        object.__setattr__(self, "available_at", require_utc(self.available_at))


@dataclass
class ReadContext:
    """What a strategy may use: the bound clock and as-of store."""

    clock: Clock
    store: ClockBoundStore
    extras: dict[str, object] = field(default_factory=dict)
