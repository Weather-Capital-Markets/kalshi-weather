"""Coverage report. Required on every backtest result.

Counts of bracket-days with no book, no two-sided book, no volume, no
ask-size, plus carry-forward staleness distribution. A result without this
report is invalid. Missing is not zero: no-book and zero-volume are
different rows; neither is silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from wxmm.core.errors import EmptyPipelineError
from wxmm.core.types import BookSnapshot


@dataclass(frozen=True, slots=True)
class CoverageRow:
    venue: str
    market_id: str
    climate_day: str
    has_book: bool
    two_sided: bool
    volume: int | None
    ask_size_known: bool
    reconstructed: bool
    staleness: timedelta | None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    rows: tuple[CoverageRow, ...]

    @property
    def n_bracket_days(self) -> int:
        return len(self.rows)

    @property
    def n_no_book(self) -> int:
        return sum(1 for row in self.rows if not row.has_book)

    @property
    def n_no_two_sided(self) -> int:
        return sum(1 for row in self.rows if not row.two_sided)

    @property
    def n_no_volume(self) -> int:
        return sum(1 for row in self.rows if row.volume is None or row.volume == 0)

    @property
    def n_volume_missing(self) -> int:
        return sum(1 for row in self.rows if row.volume is None)

    @property
    def n_volume_zero(self) -> int:
        return sum(1 for row in self.rows if row.volume == 0)

    @property
    def n_no_ask_size(self) -> int:
        return sum(1 for row in self.rows if not row.ask_size_known)

    def staleness_seconds(self) -> tuple[float, ...]:
        return tuple(
            row.staleness.total_seconds() for row in self.rows if row.staleness is not None
        )


def row_from_book(
    *,
    venue: str,
    climate_day: str,
    book: BookSnapshot | None,
    market_id: str,
) -> CoverageRow:
    if book is None:
        return CoverageRow(
            venue=venue,
            market_id=market_id,
            climate_day=climate_day,
            has_book=False,
            two_sided=False,
            volume=None,
            ask_size_known=False,
            reconstructed=False,
            staleness=None,
        )
    return CoverageRow(
        venue=venue,
        market_id=book.market_id,
        climate_day=climate_day,
        has_book=True,
        two_sided=book.two_sided,
        volume=book.volume,
        ask_size_known=book.ask_size_known,
        reconstructed=book.reconstructed,
        staleness=book.staleness,
    )


def build_coverage_report(rows: list[CoverageRow] | tuple[CoverageRow, ...]) -> CoverageReport:
    if not rows:
        return CoverageReport(rows=())
    return CoverageReport(rows=tuple(rows))


def assert_non_empty_output(*, input_count: int, output_count: int, stage: str) -> None:
    """Missing is not zero: non-empty input must not collapse to empty output."""
    if input_count > 0 and output_count == 0:
        raise EmptyPipelineError(
            f"stage {stage!r} produced empty output from {input_count} input row(s)"
        )
