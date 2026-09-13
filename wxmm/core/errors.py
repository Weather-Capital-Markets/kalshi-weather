"""Typed failures. Raise; never warn; never silently drop.

Allowed to assume
    Callers distinguish missing from zero, leakage from absence, and unverified
    facts from verified ones.

Must never
    Be used as log-and-continue paths for leakage, unverified fees, or
    size-unknown fills.
"""

from __future__ import annotations


class WxmmError(Exception):
    """Base error for the wxmm package."""


class LeakageError(WxmmError):
    """A read used a record with ``available_at`` after the as-of / clock."""


class WallClockError(WxmmError):
    """Backtest-reachable code called ``datetime.now`` / ``time.time``."""


class MissingDataError(WxmmError):
    """No record was available at the requested as-of. Distinct from zero."""


class StaleBookError(WxmmError):
    """A book is marked STALE and is not readable.

    Staleness is a first-class state. Callers must not be trusted to check a
    timestamp; ``get`` raises rather than returning the last known book.
    """


class CredentialsUnavailable(WxmmError):
    """Credential loading is disabled or no key is present. Fail closed."""


class InvalidTransition(WxmmError):
    """Lifecycle transition is not in the declared one-way graph."""


class SendTokenError(WxmmError):
    """Confirmation token missing, expired, reused, or bound to a different hash."""


class FeeMismatch(WxmmError):
    """Predicted fee disagrees with the fee actually charged on a verified schedule."""


class UnreconciledBreak(WxmmError):
    """Venue vs journal break. System must HALT until a human records a resolution."""


class RateLimited(WxmmError):
    """Venue returned 429. ``retry_after`` is seconds, or None if the header was absent."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class UnverifiedFeeSchedule(WxmmError):
    """Cost model refused to price a fill because the fee schedule is unverified."""


class UnverifiedSettlementFee(UnverifiedFeeSchedule):
    """Kalshi fee on held-to-expiry / settled contracts is (verify)."""


class SourceTimestampRequired(WxmmError):
    """Ingest refused to invent ``available_at`` from wall-clock."""


class LimitBreach(WxmmError):
    """A pre-trade limit was exceeded. Message names the limit and the overrun."""

    def __init__(self, limit_name: str, actual: str, allowed: str, *, overrun: str) -> None:
        self.limit_name = limit_name
        self.actual = actual
        self.allowed = allowed
        self.overrun = overrun
        super().__init__(
            f"limit {limit_name} breached: actual={actual} allowed={allowed} by {overrun}"
        )


class UnverifiedFactError(WxmmError):
    """An executable claim required a fact that is unverified or expired."""


class ConfigNotPreregistered(WxmmError):
    """Backtest ``run()`` saw a config hash that is not in ``prereg/``."""


class GoNoGoNotFilled(WxmmError):
    """A study ``run()`` was invoked while prereg go/no-go numbers are FILL_IN."""


class PriceComplementError(WxmmError):
    """A trade's yes_price + no_price is not ≈ 1.00. Silent acceptance inverts P&L."""


class InconsistentTakerMapping(WxmmError):
    """taker_outcome_side × taker_book_side is not a clean one-to-one map."""


class HeldOutLockedError(WxmmError):
    """Held-out period was requested without an explicit loud unlock."""


class ReconstructionBoundRequired(WxmmError):
    """``model.fit`` and market-mid baseline refuse without a Phase 3 bound file."""


class SizeUnknownFlag(WxmmError):
    """A size-dependent claim was attempted without ask/bid size.

    Results that need size must be flagged ``SIZE_UNKNOWN`` and excluded from
    executable claims — never guessed. This error is raised when code tries to
    treat unknown size as a fillable quantity.
    """


class NonFungibleNettingError(WxmmError):
    """Attempted to net two instruments whose ``Underlying`` values differ."""


class ResidualBasisRejected(WxmmError):
    """Hedge residual basis exceeded ``max_basis_risk_pct``."""


class EmptyPipelineError(WxmmError):
    """A stage produced empty output when non-empty input implied output."""


class CoverageReportMissing(WxmmError):
    """A backtest result was emitted without a coverage report."""


class TaggedAssumption:
    """Marker that a value is a tagged assumption, not a ratified fact.

    The unspecified Kalshi settlement era (through 2021-12-25) defaults to the
    10:00 AM ET snapshot rule under this marker. Do not treat it as verified.
    """

    __slots__ = ("value", "assumption_id", "note")

    def __init__(self, value: object, *, assumption_id: str, note: str) -> None:
        self.value = value
        self.assumption_id = assumption_id
        self.note = note

    def __repr__(self) -> str:
        return f"TaggedAssumption({self.value!r}, id={self.assumption_id!r})"
