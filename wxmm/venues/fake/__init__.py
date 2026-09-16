"""Fake venue package.

Allowed to assume
    Tests and the live path drive this adapter with recorded fixtures. No
    network. No credentials.

Must never
    Read the environment. Call a real Kalshi or Polymarket endpoint.
    Invent a fee schedule for Polymarket.
"""

from __future__ import annotations
