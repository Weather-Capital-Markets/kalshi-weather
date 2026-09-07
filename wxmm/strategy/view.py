"""Frozen value objects a strategy may see. No store, venue, settlement, or clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class BookView:
    """Book state at the harness clock, already filtered as-of-safe."""

    venue: str
    market_id: str
    two_sided: bool
    bid_cents: int | None
    ask_cents: int | None
    bid_size: int | None
    ask_size: int | None
    ask_size_known: bool
    volume: int | None
    reconstructed: bool
    staleness: timedelta


@dataclass(frozen=True, slots=True)
class PositionView:
    venue: str
    market_id: str
    quantity: int
    avg_price_cents: int | None


@dataclass(frozen=True, slots=True)
class FillView:
    venue: str
    market_id: str
    side: str
    price_cents: int
    quantity: int


@dataclass(frozen=True, slots=True)
class MarketView:
    """ONLY what a strategy may see. Harness-constructed. Frozen.

    Contains books (with staleness), the strategy's own positions, and its
    own realised fills. Nothing else — no store handle, no venue adapter,
    no settlement, no clock.
    """

    books: tuple[BookView, ...]
    positions: tuple[PositionView, ...]
    fills: tuple[FillView, ...]


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    """Proposal only. The human sends. ``rationale`` is required."""

    venue: str
    market: str
    side: str
    price: int
    size: int
    rationale: str

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"side must be buy|sell, not {self.side!r}")
        if not self.rationale.strip():
            raise ValueError("ProposedOrder.rationale is required")
        if self.size <= 0:
            raise ValueError("ProposedOrder.size must be positive")
