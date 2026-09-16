"""Underlying identity. Fungible iff all four fields match AND none is unverified.

Allowed to assume
    Kalshi NYC daily high settles on KNYC / CLINYC / ignore-after-snapshot /
    NWS CLI LST. Polymarket NYC daily high settles on KLGA / Weather
    Underground Daily Observations / accept-until-next-first-datapoint.
    Polymarket **day convention is (verify)** — WU Daily Observations
    presumably local civil day, which differs from CLI LST by 1 h during EDT.

Must never
    Treat Kalshi NYC and Polymarket NYC as the same underlying. Treat an
    unverified field as equal to anything, including itself. Net across
    non-fungible underlyings.
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_CONVENTION_NWS_CLI_LST = "nws_cli_lst"
# (verify) WU day convention is not known to be CLI LST. Unverified fields
# are not equal to anything, including themselves — see fungible().
DAY_CONVENTION_WU_UNVERIFIED = "UNVERIFIED:(verify)wunderground_daily_observations_civil_vs_lst"

REVISION_IGNORE_AFTER_SNAPSHOT = "ignore_after_snapshot"
REVISION_ACCEPT_UNTIL_NEXT_FIRST_DATAPOINT = "accept_until_next_first_datapoint"

PRODUCT_DAILY_HIGH = "daily_high"

UNVERIFIED_PREFIX = "UNVERIFIED:"


def field_is_unverified(value: str) -> bool:
    return value.startswith(UNVERIFIED_PREFIX)


@dataclass(frozen=True, slots=True)
class Underlying:
    """Settlement identity of an instrument.

    Two instruments are fungible iff these four fields are equal AND no
    field is tagged unverified. An unverified field is not equal to
    anything, including itself.
    """

    station: str
    product: str
    day_convention: str
    revision_rule: str

    def fungible(self, other: Underlying) -> bool:
        for value in (
            self.station,
            self.product,
            self.day_convention,
            self.revision_rule,
            other.station,
            other.product,
            other.day_convention,
            other.revision_rule,
        ):
            if field_is_unverified(value):
                return False
        return (
            self.station == other.station
            and self.product == other.product
            and self.day_convention == other.day_convention
            and self.revision_rule == other.revision_rule
        )


KALSHI_NYC_DAILY_HIGH = Underlying(
    station="KNYC",
    product=PRODUCT_DAILY_HIGH,
    day_convention=DAY_CONVENTION_NWS_CLI_LST,
    revision_rule=REVISION_IGNORE_AFTER_SNAPSHOT,
)

POLYMARKET_NYC_DAILY_HIGH = Underlying(
    station="KLGA",
    product=PRODUCT_DAILY_HIGH,
    day_convention=DAY_CONVENTION_WU_UNVERIFIED,
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
