"""Measure package: shadow fills, adverse selection, fee verification.

Allowed to assume
    Paper fills use B1 ``last_in_queue``. Real fills come from the journal.
    Kalshi's fee formula is documented; Polymarket's is not.

Must never
    Report a lone mean mark-out. Assert Polymarket fees while unverified.
    Import live transports. Guess size.
"""

from __future__ import annotations
