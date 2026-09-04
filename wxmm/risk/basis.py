"""Empirical KLGA−KNYC settlement-difference DISTRIBUTION. Never a point estimate.

Numbers are the given IEM ASOS sample (2021-01-01 … 2026-08-19, n=2057).
Do not re-derive. DJF 0.0% disagreement is a binning artifact, not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wxmm.core.underlying import Underlying

N_DAYS = 2057
DELTA_MEDIAN_F = Decimal("1.0")
KLGA_WARMER_OVERALL = Decimal("0.541")
KLGA_WARMER_JJA = Decimal("0.676")

BRACKET_DISAGREE = {
    "overall": Decimal("0.233"),
    "middle_39_85": Decimal("0.212"),
    "JJA": Decimal("0.615"),
    "SON": Decimal("0.185"),
    "MAM": Decimal("0.116"),
    "DJF": Decimal("0.000"),
}

MEASURED_BRACKET_DISAGREE_KEYS = frozenset({"JJA", "middle_39_85"})
ARTIFACT_BRACKET_DISAGREE_KEYS = frozenset({"DJF"})

SOURCE = (
    "IEM ASOS 2021-01-01..2026-08-19 n=2057; delta=round(max KLGA)-round(max KNYC); "
    "even-edged 2F Polymarket-style bins. Stage B1 given facts, not re-derived."
)


@dataclass(frozen=True, slots=True)
class SettlementDeltaDistribution:
    """Distribution of KLGA−KNYC whole-°F settlement difference.

    ``point_estimate`` does not exist. Callers must use median/shares plus
    ``bracket_disagreement_rate``.
    """

    n: int
    median_f: Decimal
    share_klga_warmer: Decimal
    bracket_disagreement_rate: Decimal
    regime: str
    measured: bool
    source: str

    def point_estimate(self) -> None:
        raise TypeError(
            "BasisModel returns a distribution, never a point estimate "
            f"(median_f={self.median_f} is a summary, not a hedge ratio)"
        )


class BasisModel:
    """First implementation: empirical seasonal / regime table from given IEM numbers."""

    def __init__(self, left: Underlying, right: Underlying) -> None:
        self.left = left
        self.right = right

    def settlement_difference(
        self,
        *,
        season: str,
        knyc_max_band: str | None = None,
    ) -> SettlementDeltaDistribution:
        if knyc_max_band == "middle_39_85":
            key = "middle_39_85"
            warmer = KLGA_WARMER_OVERALL
            measured = True
        elif season == "JJA":
            key = "JJA"
            warmer = KLGA_WARMER_JJA
            measured = True
        elif season in BRACKET_DISAGREE:
            key = season
            warmer = KLGA_WARMER_OVERALL
            measured = key in MEASURED_BRACKET_DISAGREE_KEYS
        else:
            key = "overall"
            warmer = KLGA_WARMER_OVERALL
            measured = True
        return SettlementDeltaDistribution(
            n=N_DAYS,
            median_f=DELTA_MEDIAN_F,
            share_klga_warmer=warmer,
            bracket_disagreement_rate=BRACKET_DISAGREE[key],
            regime=key,
            measured=measured,
            source=SOURCE,
        )

    def bracket_disagreement_rate(
        self,
        *,
        season: str,
        knyc_max_band: str | None = None,
    ) -> Decimal:
        dist = self.settlement_difference(season=season, knyc_max_band=knyc_max_band)
        return dist.bracket_disagreement_rate
