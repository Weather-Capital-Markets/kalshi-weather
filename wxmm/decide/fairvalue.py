"""Fair-value Protocol. One implementation: NullFairValue.

Allowed to assume
    Callers handle ``None``. The project currently has no model.

Must never
    Invent a probability. Import ``wxmm.execute``. Treat missing FV as 50%.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from wxmm.strategy.view import MarketView

MarketId = str
Probability = str  # Decimal-compatible string; no float fair values


class FairValueProvider(Protocol):
    def fair(self, view: MarketView) -> Mapping[MarketId, Probability] | None:
        ...


class NullFairValue:
    """The only shipped implementation. Always returns None."""

    def fair(self, view: MarketView) -> Mapping[MarketId, Probability] | None:
        _ = view
        return None
