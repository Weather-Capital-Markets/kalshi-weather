"""Last-in-queue fills. Never guess size."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from wxmm.core.errors import SizeUnknownFlag
from wxmm.core.types import BookLevel, BookSnapshot, Order, Trade


@dataclass(frozen=True, slots=True)
class Fill:
    order: Order
    filled_qty: int
    price_cents: int
    status: str
    reconstructed: bool
    staleness: timedelta
    queue_ahead: int | None

    def assert_executable(self) -> None:
        if self.status == "SIZE_UNKNOWN":
            raise SizeUnknownFlag(
                f"fill for {self.order.market_id} needs size; flagged SIZE_UNKNOWN, "
                "excluded from executable claims"
            )


def _level_size(levels: tuple[BookLevel, ...], price_cents: int) -> int | None:
    for level in levels:
        if level.price_cents == price_cents:
            return level.size
    return 0


def last_in_queue_fill(
    order: Order,
    book: BookSnapshot,
    trades: tuple[Trade, ...] | list[Trade],
) -> Fill:
    """Fill a resting order only after the displayed queue at the level trades.

    Last-in-queue: we stand behind ``level.size``. Volume at our price consumes
    the queue first; volume that prints strictly through the level fills us in
    full. Missing size → ``SIZE_UNKNOWN``, never a guessed quantity.
    """
    needs_ask = order.side == "buy"
    size_known = book.ask_size_known if needs_ask else all(
        level.size is not None for level in book.bids
    )
    levels = book.asks if needs_ask else book.bids
    displayed = _level_size(levels, order.price_cents)
    if not size_known or displayed is None:
        return Fill(
            order=order,
            filled_qty=0,
            price_cents=order.price_cents,
            status="SIZE_UNKNOWN",
            reconstructed=book.reconstructed,
            staleness=book.staleness,
            queue_ahead=None,
        )

    queue_ahead = displayed
    at_level = 0
    through = 0
    for trade in trades:
        if trade.market_id != order.market_id:
            continue
        if order.side == "buy":
            if trade.price_cents < order.price_cents:
                through += trade.size
            elif trade.price_cents == order.price_cents:
                at_level += trade.size
        else:
            if trade.price_cents > order.price_cents:
                through += trade.size
            elif trade.price_cents == order.price_cents:
                at_level += trade.size

    if through > 0:
        filled = order.quantity
        status = "filled"
    else:
        remainder = at_level - queue_ahead
        if remainder <= 0:
            filled = 0
            status = "none"
        elif remainder >= order.quantity:
            filled = order.quantity
            status = "filled"
        else:
            filled = remainder
            status = "partial"

    return Fill(
        order=order,
        filled_qty=filled,
        price_cents=order.price_cents,
        status=status,
        reconstructed=book.reconstructed,
        staleness=book.staleness,
        queue_ahead=queue_ahead,
    )
