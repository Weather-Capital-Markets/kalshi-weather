"""Monitor package: system status and structured alerts. No UI.

Allowed to assume
    HALT blocks proposal emission. Leaving HALT requires a journal reason.

Must never
    Auto-resume. Emit proposals while HALT. Import a web UI.
"""

from __future__ import annotations
