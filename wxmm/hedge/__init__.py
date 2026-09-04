"""Hedge package: plan legs, execute by hand, live residual.

Allowed to assume
    B1 ``HedgePlan`` is the planning primitive. Basis is re-evaluated at
    execution. Legs still go through the send gate.

Must never
    Auto-send. Net KNYC against KLGA. Report residual 0 across underlyings.
    Reuse a stale basis from plan time.
"""

from __future__ import annotations
