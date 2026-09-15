"""Fair-value evaluation: baselines and scores. Results, not defence.

Allowed to assume
    Settlement labels are the training target. Market-mid baseline requires a
    registered Phase 3 reconstruction bound.

Must never
    Report a lone mean mark-out. Train on contemporaneous mid as label.
"""

from __future__ import annotations
