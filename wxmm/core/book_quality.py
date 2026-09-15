"""Book quality defects. ``BOOK_RECONSTRUCTED`` and ``SIZE_UNKNOWN`` are distinct.

Allowed to assume
    Carry-forward reconstruction records ``staleness`` and policy. Missing
    ask/bid size is a separate defect on fills and size-dependent claims.

Must never
    Merge reconstruction with size-unknown. Treat reconstruction as verified
    without a registered Phase 3 error bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum


class BookDefectKind(str, Enum):
    """Distinct defect classes. Do not collapse these in coverage or FV."""

    SIZE_UNKNOWN = "SIZE_UNKNOWN"
    BOOK_RECONSTRUCTED = "BOOK_RECONSTRUCTED"


@dataclass(frozen=True, slots=True)
class BookReconstructed:
    """Carry-forward book state. Not the same defect as ``SIZE_UNKNOWN``.

    ``staleness`` is always recorded when ``reconstructed`` is true on the
    underlying ``BookView`` / ``BookSnapshot``.
    """

    staleness: timedelta
    policy: str = "last_quote"

    def defect_kind(self) -> BookDefectKind:
        return BookDefectKind.BOOK_RECONSTRUCTED


@dataclass(frozen=True, slots=True)
class BookSizeUnknown:
    """Ask or bid size absent. Fill simulations are upper bounds only."""

    side: str  # "bid" | "ask" | "both"

    def defect_kind(self) -> BookDefectKind:
        return BookDefectKind.SIZE_UNKNOWN


def reconstruction_from_book(
    *,
    reconstructed: bool,
    staleness: timedelta,
    policy: str = "last_quote",
) -> BookReconstructed | None:
    if not reconstructed:
        return None
    return BookReconstructed(staleness=staleness, policy=policy)
