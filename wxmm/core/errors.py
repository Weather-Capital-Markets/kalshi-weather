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


class UnverifiedFeeSchedule(WxmmError):
    """Cost model refused to price a fill because the fee schedule is unverified."""


class UnverifiedFactError(WxmmError):
    """An executable claim required a fact that is unverified or expired."""


class ConfigNotPreregistered(WxmmError):
    """Backtest ``run()`` saw a config hash that is not in ``prereg/``."""


class HeldOutLockedError(WxmmError):
    """Held-out period was requested without an explicit loud unlock."""


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
