"""Four baselines. Market-mid is gated on the Phase 3 reconstruction bound.

Allowed to assume
    NBM ladder uses the same 9-decile linear interpolation as Session 6c
    (scale flagged uncertain). Climatology uses LST day-of-year windows.

Must never
    Run market-mid without ``require_reconstruction_bound_for_fit``. Invent
    go/no-go thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Protocol

from wxmm.core.errors import ReconstructionBoundRequired
from wxmm.fairvalue.anchor import LadderQuote, market_implied, normalised_market_ladder
from wxmm.fairvalue.reconstruction_bound import (
    ReconstructionBound,
    require_reconstruction_bound_for_fit,
)
from wxmm.strategy.view import MarketView

NBM_INTERPOLATION_UNCERTAIN = (
    "9-decile linear np.interp; RES method-sensitive; B2 Phase 2 item 2 open"
)


class Baseline(Protocol):
    @property
    def name(self) -> str: ...

    def forecast(
        self, view: MarketView, *, context: BaselineContext
    ) -> Mapping[str, Decimal] | None: ...


@dataclass(frozen=True, slots=True)
class BaselineContext:
    climate_day: str
    doy: int
    season: str
    hours_to_close: float | None
    last_trade_price_cents: dict[str, int]
    climatology_table: Mapping[int, Mapping[str, Decimal]] | None
    nbm_ladder: Mapping[str, Decimal] | None
    reconstruction_bound: ReconstructionBound | None


@dataclass(frozen=True, slots=True)
class ClimatologyBaseline:
    name: str = "climatology"

    def forecast(
        self, view: MarketView, *, context: BaselineContext
    ) -> Mapping[str, Decimal] | None:
        if context.climatology_table is None:
            return None
        row = context.climatology_table.get(context.doy)
        if row is None:
            return None
        market_ids = {book.market_id for book in view.books}
        if not market_ids <= set(row.keys()):
            return None
        return {k: row[k] for k in sorted(market_ids)}


@dataclass(frozen=True, slots=True)
class PersistenceBaseline:
    name: str = "persistence"

    def forecast(
        self, view: MarketView, *, context: BaselineContext
    ) -> Mapping[str, Decimal] | None:
        if not context.last_trade_price_cents:
            return None
        market_ids = [book.market_id for book in view.books]
        if not all(mid in context.last_trade_price_cents for mid in market_ids):
            return None
        raw = {
            mid: Decimal(context.last_trade_price_cents[mid]) / Decimal(100)
            for mid in market_ids
        }
        total = sum(raw.values(), Decimal(0))
        if total <= Decimal(0):
            return None
        return {k: v / total for k, v in raw.items()}


@dataclass(frozen=True, slots=True)
class MarketMidBaseline:
    name: str = "market_mid"

    def forecast(
        self, view: MarketView, *, context: BaselineContext
    ) -> Mapping[str, Decimal] | None:
        if context.reconstruction_bound is None:
            raise ReconstructionBoundRequired(
                "market_mid baseline requires a registered reconstruction bound in context"
            )
        ladder: LadderQuote = market_implied(view)
        try:
            return normalised_market_ladder(ladder)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class NbmLadderBaseline:
    name: str = "nbm_ladder"
    interpolation_note: str = NBM_INTERPOLATION_UNCERTAIN

    def forecast(
        self, view: MarketView, *, context: BaselineContext
    ) -> Mapping[str, Decimal] | None:
        if context.nbm_ladder is None:
            return None
        market_ids = {book.market_id for book in view.books}
        if not market_ids <= set(context.nbm_ladder.keys()):
            return None
        raw = {k: context.nbm_ladder[k] for k in sorted(market_ids)}
        total = sum(raw.values(), Decimal(0))
        if total <= Decimal(0):
            return None
        return {k: v / total for k, v in raw.items()}


def all_baselines() -> tuple[Baseline, ...]:
    baselines: list[Baseline] = [
        ClimatologyBaseline(),
        PersistenceBaseline(),
        MarketMidBaseline(),
        NbmLadderBaseline(),
    ]
    return tuple(baselines)


def context_with_bound(
    *,
    climate_day: str,
    doy: int,
    season: str,
    bound_path: Path | None = None,
    **kwargs: object,
) -> BaselineContext:
    bound = require_reconstruction_bound_for_fit(bound_path)
    return BaselineContext(
        climate_day=climate_day,
        doy=doy,
        season=season,
        hours_to_close=kwargs.get("hours_to_close"),  # type: ignore[arg-type]
        last_trade_price_cents=kwargs.get("last_trade_price_cents") or {},  # type: ignore[arg-type]
        climatology_table=kwargs.get("climatology_table"),  # type: ignore[arg-type]
        nbm_ladder=kwargs.get("nbm_ladder"),  # type: ignore[arg-type]
        reconstruction_bound=bound,
    )
