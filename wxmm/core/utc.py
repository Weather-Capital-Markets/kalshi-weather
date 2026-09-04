"""UTC-aware datetime validation. No timezone conversion to local.

Allowed to assume
    Callers pass aware datetimes.

Must never
    Convert to America/New_York or LST. That lives only in ``timeauth``.
    Call ``datetime.now``.
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def require_utc(ts: datetime) -> datetime:
    """Reject naive datetimes; return the instant as UTC-aware."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime rejected; wxmm timestamps must be UTC-aware")
    return ts.astimezone(UTC)
