"""BOOK_RECONSTRUCTED distinct from SIZE_UNKNOWN."""

from __future__ import annotations

from datetime import timedelta

from wxmm.core.book_quality import BookDefectKind
from wxmm.strategy.view import BookView


def test_reconstruction_and_size_unknown_are_distinct() -> None:
    book = BookView(
        venue="kalshi",
        market_id="M",
        two_sided=True,
        bid_cents=40,
        ask_cents=42,
        bid_size=None,
        ask_size=None,
        ask_size_known=False,
        volume=1,
        reconstructed=True,
        staleness=timedelta(seconds=90),
    )
    kinds = book.defect_kinds()
    assert BookDefectKind.BOOK_RECONSTRUCTED in kinds
    assert BookDefectKind.SIZE_UNKNOWN in kinds
    assert len(kinds) == 2
