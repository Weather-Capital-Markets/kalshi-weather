"""Maker-side fair value. Market-anchored; beta=0 is the default.

Allowed to assume
    ``MarketView`` is built only via ``wxmm.core.view.build_market_view``.
    Callers handle ``None`` fair values.

Must never
    Import ``wxmm.execute``. Auto-send. Let a TRADE_DERIVED mid satisfy
    ``ReconstructionBoundRequired``. Write a reconstructed ``book`` feature
    block or change ``model.fit`` until Stage 0 reports. Emit P&L from v0.
"""

from __future__ import annotations
