"""Fail-closed credential loading. The only module that may read the environment.

Allowed to assume
    Callers pass ``enabled=True`` explicitly when they intend to load keys.
    Tests never set API keys and never pass ``enabled=True``.

Must never
    Load keys at import time. Default ``enabled`` to True. Invent a key.
    Be imported by ``wxmm.backtest``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from wxmm.core.errors import CredentialsUnavailable

_ENV_KEYS = {
    "kalshi": ("WXMM_KALSHI_API_KEY", "WXMM_KALSHI_API_SECRET"),
    "polymarket": ("WXMM_POLYMARKET_API_KEY", "WXMM_POLYMARKET_API_SECRET"),
}


@dataclass(frozen=True, slots=True)
class VenueCredentials:
    venue: str
    api_key: str
    api_secret: str


def load_credentials(venue: str, *, enabled: bool) -> VenueCredentials:
    """Raise ``CredentialsUnavailable`` unless ``enabled`` is True and keys exist."""
    if not enabled:
        raise CredentialsUnavailable(
            f"credentials for {venue!r} unavailable: load_credentials(enabled=True) "
            "was not passed; fail closed"
        )
    pair = _ENV_KEYS.get(venue)
    if pair is None:
        raise CredentialsUnavailable(f"no credential mapping for venue {venue!r}")
    key_name, secret_name = pair
    api_key = os.environ.get(key_name)
    api_secret = os.environ.get(secret_name)
    if not api_key or not api_secret:
        raise CredentialsUnavailable(
            f"credentials for {venue!r} unavailable: {key_name}/{secret_name} unset"
        )
    return VenueCredentials(venue=venue, api_key=api_key, api_secret=api_secret)
