"""C1-X1: maker/taker returns on the public trade record.

Allowed to assume
    Kalshi identifies the aggressor on both live and historical trades
    (``taker_outcome_side``, ``taker_book_side``). Weather maker fee is $0.00
    (venue-facts §2.1, verified 2026-08-15).

Must never
    Read an order book, a reconstructed book, or a candle. Import
    ``wxmm.core.book``, ``wxmm.core.book_quality``, or ``wxmm.fairvalue``.
    Read the deprecated aggressor field. Label from METAR / GHCN-D /
    "final" climate data. Report outputs as strategy P&L.
"""

from __future__ import annotations
