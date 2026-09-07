"""Re-export. Rate-limit math lives in ``wxmm.core.ratelimit``.

Allowed to assume
    Decide consults buckets before a proposal is shown. Live may import
    these names; decide must import ``wxmm.core.ratelimit`` instead.

Must never
    Be imported by ``wxmm.decide``. Consume tokens when only peeking
    ``RATE_BLOCKED``. Call wall-clock.
"""

from __future__ import annotations

from wxmm.core.ratelimit import (
    KALSHI_BATCH_CANCEL_COST,
    KALSHI_ORDER_COST,
    KALSHI_WRITE_TOKENS_PER_SEC,
    POLYMARKET_REQUEST_COST,
    POLYMARKET_REQUESTS_PER_SEC,
    TokenBucket,
    kalshi_write_bucket,
    polymarket_public_bucket,
    proposal_cost,
)

__all__ = [
    "KALSHI_BATCH_CANCEL_COST",
    "KALSHI_ORDER_COST",
    "KALSHI_WRITE_TOKENS_PER_SEC",
    "POLYMARKET_REQUEST_COST",
    "POLYMARKET_REQUESTS_PER_SEC",
    "TokenBucket",
    "kalshi_write_bucket",
    "polymarket_public_bucket",
    "proposal_cost",
]
