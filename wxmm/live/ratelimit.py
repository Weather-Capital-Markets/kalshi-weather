"""Token buckets. Consulted before a proposal is displayed.

Allowed to assume
    Kalshi basic-tier write ≈ 100 tokens/s (≈ 10 orders/s); batch cancel
    costs 2 tokens each. Polymarket public REST is 20 req/s per IP.

Must never
    Display a sendable proposal that the bucket cannot afford. Call
    ``datetime.now``. Read credentials. Import ``wxmm.backtest``.
"""

from __future__ import annotations

from wxmm.core.types import Clock

KALSHI_WRITE_TOKENS_PER_SEC = 100.0
KALSHI_ORDER_COST = 10.0  # 100 tokens/s ≈ 10 orders/s
KALSHI_BATCH_CANCEL_COST = 2.0
POLYMARKET_REQUESTS_PER_SEC = 20.0
POLYMARKET_REQUEST_COST = 1.0


class TokenBucket:
    """Refills from the bound clock. Peek without consuming for RATE_BLOCKED."""

    def __init__(self, rate_per_sec: float, capacity: float, clock: Clock) -> None:
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate_per_sec = rate_per_sec
        self.capacity = capacity
        self._clock = clock
        self._tokens = capacity
        self._last = clock.now()

    def _refill(self) -> None:
        now = self._clock.now()
        elapsed = (now - self._last).total_seconds()
        if elapsed < 0:
            raise ValueError("clock moved backwards")
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
        self._last = now

    def remaining(self) -> float:
        self._refill()
        return self._tokens

    def can_afford(self, cost: float) -> bool:
        return self.remaining() + 1e-12 >= cost

    def consume(self, cost: float) -> bool:
        if not self.can_afford(cost):
            return False
        self._tokens -= cost
        return True


def kalshi_write_bucket(clock: Clock) -> TokenBucket:
    return TokenBucket(KALSHI_WRITE_TOKENS_PER_SEC, KALSHI_WRITE_TOKENS_PER_SEC, clock)


def polymarket_public_bucket(clock: Clock) -> TokenBucket:
    return TokenBucket(POLYMARKET_REQUESTS_PER_SEC, POLYMARKET_REQUESTS_PER_SEC, clock)


def proposal_cost(venue: str, *, batch_cancel: bool = False) -> float:
    if venue == "kalshi":
        return KALSHI_BATCH_CANCEL_COST if batch_cancel else KALSHI_ORDER_COST
    if venue in {"polymarket", "fake"}:
        return POLYMARKET_REQUEST_COST if venue == "polymarket" else KALSHI_ORDER_COST
    raise KeyError(f"no rate-limit cost for venue {venue!r}")
