"""Fit entrypoint stub. Refuses until Stage 0 reconstruction bound is COMPLETE.

Allowed to assume
    Callers invoke ``fit`` only after prereg and Stage 0. Feature blocks and
    the full ridge MLE land after ``sweep_status == COMPLETE``.

Must never
    Fit without a COMPLETE reconstruction bound. Look at training rows before
    the gate. Import ``wxmm.execute``.

Note (errors-in-variables)
    When the book feature block and the offset ``logit(q)`` are both derived
    from the same reconstructed books, their measurement errors are correlated
    by construction. Errors-in-variables bias in beta is not signable a priori.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wxmm.fairvalue.reconstruction_bound import (
    ReconstructionBound,
    require_reconstruction_bound_for_fit,
)


def fit(
    *,
    bound_path: Path | None = None,
    **_kwargs: Any,
) -> ReconstructionBound:
    """Refuse unless Stage 0 reported ``sweep_status == COMPLETE``.

    Full estimation is intentionally absent until Stage 0 completes. This
    function exists so the run ledger's count of looks at the data stays honest:
    a call that cannot fit still raises, and is not a silent no-op.
    """
    return require_reconstruction_bound_for_fit(bound_path)
