"""Coverage: no-book vs zero-volume are different rows; neither dropped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wxmm.core.types import BookLevel, BookSnapshot
from wxmm.data.quality import build_coverage_report, row_from_book

UTC = timezone.utc


def _book(*, volume: int | None, two_sided: bool = True) -> BookSnapshot:
    ts = datetime(2026, 7, 4, 16, 0, tzinfo=UTC)
    bids = (BookLevel(40, 10),) if two_sided else ()
    asks = (BookLevel(42, 10),) if two_sided else ()
    return BookSnapshot(
        market_id="KXHIGHNY-26JUL04-T90",
        valid_at=ts,
        available_at=ts,
        bids=bids,
        asks=asks,
        volume=volume,
        ask_size_known=True,
        reconstructed=False,
        staleness=timedelta(0),
        two_sided=two_sided,
    )


def test_no_book_and_zero_volume_are_distinct_coverage_rows() -> None:
    rows = [
        row_from_book(venue="kalshi", climate_day="2026-07-04", book=None, market_id="A"),
        row_from_book(
            venue="kalshi",
            climate_day="2026-07-04",
            book=_book(volume=0),
            market_id="B",
        ),
    ]
    report = build_coverage_report(rows)
    assert report.n_bracket_days == 2
    assert report.n_no_book == 1
    assert report.n_volume_zero == 1
    assert report.n_volume_missing == 1
    assert rows[0].has_book is False and rows[0].volume is None
    assert rows[1].has_book is True and rows[1].volume == 0
    assert rows[0] != rows[1]


def test_coverage_does_not_drop_rows() -> None:
    rows = [
        row_from_book(venue="kalshi", climate_day="2026-07-04", book=None, market_id="A"),
        row_from_book(
            venue="kalshi",
            climate_day="2026-07-05",
            book=_book(volume=None, two_sided=False),
            market_id="B",
        ),
    ]
    report = build_coverage_report(rows)
    assert len(report.rows) == 2


def test_non_empty_input_cannot_collapse_silently() -> None:
    from wxmm.core.errors import EmptyPipelineError
    from wxmm.data.quality import assert_non_empty_output

    assert_non_empty_output(input_count=0, output_count=0, stage="x")
    with pytest.raises(EmptyPipelineError):
        assert_non_empty_output(input_count=3, output_count=0, stage="x")
