"""Eval baselines: climatology, persistence, NBM; market-mid gated."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from wxmm.core.errors import ReconstructionBoundRequired
from wxmm.eval.baselines import (
    BaselineContext,
    ClimatologyBaseline,
    MarketMidBaseline,
    NbmLadderBaseline,
    PersistenceBaseline,
    context_with_bound,
)
from wxmm.fairvalue import model as fairvalue_model
from wxmm.fairvalue.reconstruction_bound import ReconstructionBound, load_reconstruction_bound
from wxmm.strategy.view import BookView, MarketView


def _view() -> MarketView:
    from datetime import timedelta

    return MarketView(
        books=(
            BookView(
                venue="kalshi",
                market_id="A",
                two_sided=True,
                bid_cents=40,
                ask_cents=42,
                bid_size=None,
                ask_size=None,
                ask_size_known=False,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
            BookView(
                venue="kalshi",
                market_id="B",
                two_sided=True,
                bid_cents=50,
                ask_cents=52,
                bid_size=None,
                ask_size=None,
                ask_size_known=False,
                volume=1,
                reconstructed=False,
                staleness=timedelta(0),
            ),
        ),
        positions=(),
        fills=(),
    )


def _bound() -> ReconstructionBound:
    return ReconstructionBound(
        branch="full_corpus",
        interval_anchor="interval_end",
        timezone="UTC",
        bucket_offset=0,
        match_rate=0.85,
        silent_count=100,
        boundaries_compared=1000,
        inputs_manifest="analysis/out/emission_convention_sweep_inputs.json",
        measured_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        winning_convention_found=True,
        reconstruction_error_bound="carry_forward_last_quote@interval_end|UTC|+0",
        sweep_status="COMPLETE",
    )


def _complete_artifact(path: Path, *, match_rate: float = 0.85) -> Path:
    rows: list[dict[str, object]] = []
    for interval_anchor in ("interval_start", "interval_end"):
        for tz in ("UTC", "ET"):
            for offset in (-1, 0, 1):
                key = f"{interval_anchor}|{tz}|{offset:+d}"
                is_winner = offset == 0 and tz == "UTC" and interval_anchor == "interval_end"
                rows.append(
                    {
                        "interval_anchor": interval_anchor,
                        "timezone": tz,
                        "bucket_offset": offset,
                        "key": key,
                        "boundaries_compared": 1000,
                        "matched": int(round((match_rate if is_winner else 0.01) * 1000)),
                        "match_rate": match_rate if is_winner else 0.01,
                        "silent_count": 10,
                        "row_status": "computed",
                    }
                )
    winner = max(rows, key=lambda r: float(r["match_rate"]))  # type: ignore[arg-type]
    payload = {
        "measured_at": "2026-09-04T12:00:00Z",
        "sweep_status": "COMPLETE",
        "rows_computed": "12/12",
        "branch": "full_corpus",
        "winning_convention_found": True,
        "reconstruction_error_bound": f"carry_forward_last_quote@{winner['key']}",
        "inputs_manifest": "analysis/out/emission_convention_sweep_inputs.json",
        "winner": winner,
        "table": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_climatology_and_persistence() -> None:
    view = _view()
    ctx = BaselineContext(
        climate_day="2026-07-04",
        doy=186,
        season="summer",
        hours_to_close=5.0,
        last_trade_price_cents={"A": 41, "B": 51},
        climatology_table={186: {"A": Decimal("0.4"), "B": Decimal("0.6")}},
        nbm_ladder=None,
        reconstruction_bound=None,
    )
    clim = ClimatologyBaseline().forecast(view, context=ctx)
    assert clim == {"A": Decimal("0.4"), "B": Decimal("0.6")}
    persist = PersistenceBaseline().forecast(view, context=ctx)
    assert persist is not None
    assert abs(sum(persist.values(), Decimal(0)) - Decimal(1)) < Decimal("1e-9")


def test_market_mid_refuses_without_bound() -> None:
    view = _view()
    ctx = BaselineContext(
        climate_day="2026-07-04",
        doy=186,
        season="summer",
        hours_to_close=5.0,
        last_trade_price_cents={},
        climatology_table=None,
        nbm_ladder=None,
        reconstruction_bound=None,
    )
    with pytest.raises(ReconstructionBoundRequired):
        MarketMidBaseline().forecast(view, context=ctx)


def test_market_mid_with_bound() -> None:
    view = _view()
    ctx = BaselineContext(
        climate_day="2026-07-04",
        doy=186,
        season="summer",
        hours_to_close=5.0,
        last_trade_price_cents={},
        climatology_table=None,
        nbm_ladder=None,
        reconstruction_bound=_bound(),
    )
    forecast = MarketMidBaseline().forecast(view, context=ctx)
    assert forecast is not None
    assert abs(sum(forecast.values(), Decimal(0)) - Decimal(1)) < Decimal("1e-9")


def test_nbm_ladder_normalises() -> None:
    view = _view()
    ctx = BaselineContext(
        climate_day="2026-07-04",
        doy=186,
        season="summer",
        hours_to_close=None,
        last_trade_price_cents={},
        climatology_table=None,
        nbm_ladder={"A": Decimal("0.3"), "B": Decimal("0.4")},
        reconstruction_bound=None,
    )
    forecast = NbmLadderBaseline().forecast(view, context=ctx)
    assert forecast is not None
    assert abs(sum(forecast.values(), Decimal(0)) - Decimal(1)) < Decimal("1e-9")


def test_incomplete_committed_artifact_still_raises_on_fit_and_market_mid() -> None:
    """Gate must not unlock merely because the JSON file exists."""
    path = Path("analysis/out/emission_convention_sweep.json")
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw.get("sweep_status") == "INCOMPLETE"
    assert "branch" not in raw

    with pytest.raises(ReconstructionBoundRequired, match="INCOMPLETE"):
        load_reconstruction_bound(path)

    with pytest.raises(ReconstructionBoundRequired, match="INCOMPLETE"):
        fairvalue_model.fit(bound_path=path)

    with pytest.raises(ReconstructionBoundRequired, match="INCOMPLETE"):
        context_with_bound(
            climate_day="2026-07-04",
            doy=186,
            season="summer",
            bound_path=path,
        )


def test_complete_artifact_unlocks_fit_and_market_mid(tmp_path: Path) -> None:
    path = _complete_artifact(tmp_path / "emission_convention_sweep.json")
    bound = fairvalue_model.fit(bound_path=path)
    assert bound.sweep_status == "COMPLETE"
    assert bound.branch == "full_corpus"
    assert bound.reconstruction_error_bound.startswith("carry_forward_last_quote@")

    ctx = context_with_bound(
        climate_day="2026-07-04",
        doy=186,
        season="summer",
        bound_path=path,
    )
    forecast = MarketMidBaseline().forecast(_view(), context=ctx)
    assert forecast is not None
