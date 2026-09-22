"""Settlement rule object and observation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from wxmm.core.underlying import Underlying
from wxmm.core.utc import require_utc


@dataclass(frozen=True, slots=True)
class Observation:
    """A station daily-high report as of a publication instant.

    ``high_f`` is ``None`` when the source prints missing (e.g. CLINYC ``MM``).
    That is not zero.
    """

    station: str
    climate_day: date
    high_f: int | None
    valid_at: datetime
    available_at: datetime
    source: str
    is_full_day: bool
    is_revision: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_at", require_utc(self.valid_at))
        object.__setattr__(self, "available_at", require_utc(self.available_at))


@dataclass(frozen=True, slots=True)
class SettlementRule:
    venue: str
    underlying: Underlying
    rule_id: str
    effective_from: date
    effective_to: date | None
    snapshot_policy: str
    revision_policy: str
    tagged_assumption: str | None = None

    def covers(self, climate_day: date) -> bool:
        if climate_day < self.effective_from:
            return False
        if self.effective_to is not None and climate_day > self.effective_to:
            return False
        return True


@dataclass(frozen=True, slots=True)
class RevisionNotice:
    """Post-snapshot full-day issuances visible at ``as_of``.

    Kalshi settlement ignores these: ``high_f`` stays the snapshot value.
    A notice with ``n_later > 0`` is how the layer records that it saw them.
    """

    n_later: int
    later_highs: tuple[int | None, ...]
    high_changed: bool
    ignored_for_resolution: bool = True


@dataclass(frozen=True, slots=True)
class SettlementResult:
    climate_day: date
    venue: str
    rule_id: str
    high_f: int | None
    snapshot_at: datetime | None
    observation_available_at: datetime | None
    tagged_assumption: str | None
    pending: bool
    source: str
    revision: RevisionNotice | None = None
