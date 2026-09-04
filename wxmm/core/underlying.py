"""Underlying identity. Fungible iff all four fields match.

Allowed to assume
    Kalshi NYC daily high settles on KNYC / CLINYC / ignore-after-snapshot.
    Polymarket NYC daily high settles on KLGA / Weather Underground Daily
    Observations / accept-until-next-first-datapoint.
    (``knowledge/venue-facts.md`` §3.2.)

Must never
    Treat Kalshi NYC and Polymarket NYC as the same underlying. Must never
    net across non-fungible underlyings. Must never assume Weather Underground
    day convention equals NWS CLI LST — the Polymarket day convention is a
    distinct string (verify).
"""

from __future__ import annotations

from dataclasses import dataclass

# Day conventions. Do not collapse these; they are part of Underlying identity.
DAY_CONVENTION_NWS_CLI_LST = "nws_cli_lst"
DAY_CONVENTION_WU_DAILY_OBS = "wunderground_daily_observations"  # (verify) ≠ CLI LST

REVISION_IGNORE_AFTER_SNAPSHOT = "ignore_after_snapshot"
REVISION_ACCEPT_UNTIL_NEXT_FIRST_DATAPOINT = "accept_until_next_first_datapoint"

PRODUCT_DAILY_HIGH = "daily_high"


@dataclass(frozen=True, slots=True)
class Underlying:
    """Settlement identity of an instrument.

    Two instruments are fungible iff these four fields are equal. Anything
    else is a basis position, never assumed hedgeable at 1:1.
    """

    station: str
    product: str
    day_convention: str
    revision_rule: str

    def fungible(self, other: Underlying) -> bool:
        return self == other


KALSHI_NYC_DAILY_HIGH = Underlying(
    station="KNYC",
    product=PRODUCT_DAILY_HIGH,
    day_convention=DAY_CONVENTION_NWS_CLI_LST,
    revision_rule=REVISION_IGNORE_AFTER_SNAPSHOT,
)

POLYMARKET_NYC_DAILY_HIGH = Underlying(
    station="KLGA",
    product=PRODUCT_DAILY_HIGH,
    day_convention=DAY_CONVENTION_WU_DAILY_OBS,
    revision_rule=REVISION_ACCEPT_UNTIL_NEXT_FIRST_DATAPOINT,
)


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable contract that carries an ``Underlying``."""

    venue: str
    market_id: str
    underlying: Underlying
    climate_day: str | None = None
    label: str | None = None


class UnderlyingRegistry:
    """Maps ``(venue, market_id)`` → ``Underlying``.

    Nothing above the venue-adapter layer should import a venue module;
    it should look up underlyings here.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Underlying] = {}

    def register(self, venue: str, market_id: str, underlying: Underlying) -> None:
        key = (venue, market_id)
        existing = self._items.get(key)
        if existing is not None and existing != underlying:
            raise ValueError(f"conflicting underlying for {venue}:{market_id}")
        self._items[key] = underlying

    def get(self, venue: str, market_id: str) -> Underlying:
        try:
            return self._items[(venue, market_id)]
        except KeyError as exc:
            raise KeyError(f"no underlying registered for {venue}:{market_id}") from exc

    def default_nyc(self) -> None:
        """Register the two known NYC daily-high underlyings by venue name."""
        self.register("kalshi", "KXHIGHNY", KALSHI_NYC_DAILY_HIGH)
        self.register("kalshi", "HIGHNY", KALSHI_NYC_DAILY_HIGH)
        self.register("polymarket", "nyc-daily-weather", POLYMARKET_NYC_DAILY_HIGH)
