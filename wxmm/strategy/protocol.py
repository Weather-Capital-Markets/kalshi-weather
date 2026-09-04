"""Strategy Protocol. Zero implementations in this module."""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from wxmm.core.types import Order, ReadContext


@runtime_checkable
class Strategy(Protocol):
    """Propose orders. The human sends them. This package never routes."""

    def on_event(self, ctx: ReadContext) -> Sequence[Order]:
        """Return proposed intents for this clock instant. May be empty."""
        ...
