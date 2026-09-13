"""Maker-side fair value. Market-anchored; beta=0 is the default.

Allowed to assume
    ``MarketView`` is built only via ``wxmm.core.view.build_market_view``.
    Callers handle ``None`` fair values.

Must never
    Import ``wxmm.execute``. Auto-send. Fit without a Phase 3 reconstruction
    bound. Write ``features.py`` book block or ``model.py`` until Stage 0
    reports.
"""

from __future__ import annotations
