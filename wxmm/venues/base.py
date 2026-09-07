"""Venue Protocol. One protocol, two adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from wxmm.core.money import Money
from wxmm.core.types import AsOfStore, BookSnapshot, InMemoryAsOfStore, Order, Trade
from wxmm.settlement.rules import SettlementRule

_FEE_STORE = InMemoryAsOfStore()


@runtime_checkable
class Venue(Protocol):
    name: str

    def list_markets(self, as_of: datetime) -> tuple[str, ...]:
        """Markets known at ``as_of``. No 'latest'."""
        ...

    def book_at(self, market: str, as_of: datetime) -> BookSnapshot:
        """Book readable at ``as_of``. LeakageError if the record is not yet available."""
        ...

    def trades(
        self,
        market: str,
        window: tuple[datetime, datetime],
        as_of: datetime,
    ) -> tuple[Trade, ...]:
        """Trades in ``window`` that were available at ``as_of``."""
        ...

    def fee(self, order: Order) -> Money:
        """Exact fee or ``UnverifiedFeeSchedule``. Never float."""
        ...

    def rate_limits(self) -> dict[str, float]:
        ...

    def settlement_rule(self, as_of: datetime) -> SettlementRule:
        ...


def get_venue(name: str, store: AsOfStore) -> Venue:
    """Factory so callers never import ``wxmm.venues.kalshi`` / ``polymarket``."""
    if name == "kalshi":
        from wxmm.venues.kalshi.adapter import KalshiVenue

        return KalshiVenue(store)
    if name == "polymarket":
        from wxmm.venues.polymarket.adapter import PolymarketVenue

        return PolymarketVenue(store)
    if name == "fake":
        from wxmm.venues.fake.adapter import FakeVenue

        return FakeVenue(store)
    raise KeyError(f"unknown venue {name!r}; registry accepts a third only when facts exist")


def modelled_fee(venue_name: str, order: Order) -> Money:
    """Fee from the venue adapter. Polymarket raises ``UnverifiedFeeSchedule``."""
    name = "kalshi" if venue_name == "fake" else venue_name
    return get_venue(name, _FEE_STORE).fee(order)
