"""Point-in-time settlement. Versioned rules with effective dates.

Allowed to assume
    Kalshi: KNYC / CLINYC / ignore-after-snapshot; snapshot era table in
    ``knowledge/venue-facts.md`` §1.10. Unspecified era (through 2021-12-25)
    defaults to 10:00 AM ET as a **tagged assumption** (V1), not a fact.
    Polymarket: KLGA / Weather Underground Daily Observations / accept
    revisions until the next day's first datapoint.

Must never
    Apply one fixed snapshot hour across the archive. Invent a 7:00 vs 8:00
    AM choice as a verified fact (the current-era phrase is \"first 7:00 or
    8:00 AM ET\"; see eras). Treat missing high_F as 0. Convert timezones
    itself — call ``wxmm.core.timeauth``.
"""

from __future__ import annotations
