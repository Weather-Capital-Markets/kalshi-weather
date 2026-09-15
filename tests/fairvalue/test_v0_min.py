"""C1-M1 v0-MINIMAL: walk-forward, MLE null recovery, clustered CI, coverage."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    bootstrap_mean_ci,
    clustered_bootstrap_mean_ci,
)
from wxmm.analysis.trades_ingest import parse_trade
from wxmm.backtest.ledger import Ledger
from wxmm.core.errors import LeakageError
from wxmm.fairvalue.anchor_trades import refuse_if_leaked
from wxmm.fairvalue.model_trades import null_trade_recovery, trade_ladder_or_none
from wxmm.fairvalue.v0_min import (
    climatology_forecast,
    fit_offset_logit_mle,
    predict_adjusted,
    residual_gbm_experiment,
    run_c1_m1_v0_min,
    trade_anchor_coverage,
)
from wxmm.fairvalue.walkforward import assert_schedule_integrity, expanding_origins
from wxmm.settlement.eras import kalshi_last_trading_close_utc

UTC = timezone.utc
MONTHS = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}


def _ticker(day: date, strike: str) -> str:
    return f"KXHIGHNY-{day.strftime('%y')}{MONTHS[day.month]}{day.strftime('%d')}-{strike}"


def _raw(
    *,
    trade_id: str,
    ticker: str,
    outcome: str,
    book: str,
    yes: str,
    no: str,
    created: str,
) -> object:
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": "10.00",
            "yes_price_dollars": yes,
            "no_price_dollars": no,
            "taker_outcome_side": outcome,
            "taker_book_side": book,
            "created_time": created,
            "is_block_trade": False,
        },
        source_endpoint="historical",
    )


def _pair_for_day(day: date, *, hour: int = 1) -> list[object]:
    created = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    t80 = _ticker(day, "T80")
    t90 = _ticker(day, "T90")
    return [
        _raw(
            trade_id=f"{t80}-y",
            ticker=t80,
            outcome="yes",
            book="ask",
            yes="0.4500",
            no="0.5500",
            created=created,
        ),
        _raw(
            trade_id=f"{t80}-n",
            ticker=t80,
            outcome="no",
            book="bid",
            yes="0.3500",
            no="0.6500",
            created=created,
        ),
        _raw(
            trade_id=f"{t90}-y",
            ticker=t90,
            outcome="yes",
            book="ask",
            yes="0.5500",
            no="0.4500",
            created=created,
        ),
        _raw(
            trade_id=f"{t90}-n",
            ticker=t90,
            outcome="no",
            book="bid",
            yes="0.4000",
            no="0.6000",
            created=created,
        ),
    ]


def _labels_for_days(days: list[date], winner: str = "T80") -> dict[str, SettlementLabel]:
    out: dict[str, SettlementLabel] = {}
    for day in days:
        for strike in ("T80", "T90"):
            ticker = _ticker(day, strike)
            out[ticker] = SettlementLabel(
                ticker=ticker,
                climate_day=day,
                yes_won=(strike == winner),
                source="clinyc_as_issued",
            )
    return out


def test_walkforward_expanding_and_as_of() -> None:
    days = [date(2026, 8, d) for d in range(10, 16)]
    origins = list(expanding_origins(days, min_train_days=2, hours_to_close=(24, 12)))
    assert_schedule_integrity(origins)
    predict_days = sorted({o.predict_day for o in origins})
    assert predict_days[0] == date(2026, 8, 12)
    for origin in origins:
        assert max(origin.train_days) < origin.predict_day
        close = kalshi_last_trading_close_utc(origin.predict_day)
        assert origin.as_of == close - timedelta(hours=origin.hours_to_close)
        assert origin.hours_to_close in (24, 12)
    lengths = [len(o.train_days) for o in origins if o.hours_to_close == 24]
    assert lengths == sorted(lengths)
    assert lengths[-1] > lengths[0]


def test_null_mle_recovers_ladder_and_mutant_goes_red() -> None:
    day = date(2026, 8, 12)
    trades = _pair_for_day(day)
    tickers = [_ticker(day, "T80"), _ticker(day, "T90")]
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=12)
    ladder = trade_ladder_or_none(trades, tickers, as_of=as_of)
    assert ladder is not None
    q = null_trade_recovery(ladder)
    assert abs(sum(q.values(), Decimal("0")) - Decimal("1")) < Decimal("1e-9")
    recovered = predict_adjusted(q, tickers, [[0.0], [0.0]], [0.0])
    for key in q:
        assert abs(recovered[key] - q[key]) < Decimal("1e-9")
    perturbed = predict_adjusted(q, tickers, [[1.0], [-1.0]], [0.5])
    assert any(abs(perturbed[k] - q[k]) > Decimal("1e-6") for k in q)
    raw_total = sum((book.mid or Decimal("0")) for book in ladder.books.values())
    assert abs(raw_total - Decimal("1")) > Decimal("1e-6")
    # Mutation: returning un-normalised mids must not pass as β = 0.
    assert any(abs(q[k] - (ladder.books[k].mid or Decimal("0"))) > Decimal("1e-9") for k in q)


def test_as_of_refuse_if_leaked() -> None:
    day = date(2026, 8, 12)
    late = _pair_for_day(day, hour=23)
    as_of = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    with pytest.raises(LeakageError, match="available_at"):
        refuse_if_leaked(late, as_of)  # type: ignore[arg-type]


def test_day_clustered_ci_wider_than_trade_level() -> None:
    groups: dict[date, list[Decimal]] = {
        date(2026, 7, 1): [Decimal("0")] * 8,
        date(2026, 7, 2): [Decimal("1")] * 8,
        date(2026, 7, 3): [Decimal("2")] * 8,
        date(2026, 7, 4): [Decimal("3")] * 8,
    }
    flat = [v for vals in groups.values() for v in vals]
    clustered = clustered_bootstrap_mean_ci(groups, seed=0, n_resample=400)
    trade = bootstrap_mean_ci(flat, seed=0, n_resample=400)
    assert clustered is not None and trade is not None
    clustered_width = clustered[1] - clustered[0]
    trade_width = trade[1] - trade[0]
    assert clustered_width > trade_width


def test_coverage_reported_not_gated(tmp_path: Path) -> None:
    days = [date(2026, 8, d) for d in range(10, 16)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days)
    coverage = trade_anchor_coverage(
        trades,  # type: ignore[arg-type]
        labels,
        hours_to_close=(24, 12),
        mapping=None,
    )
    assert coverage.n_grid == len(days) * 2
    assert 0.0 <= coverage.share <= 1.0
    assert coverage.n_both_sides_uncrossed == coverage.n_grid


def test_run_walkforward_scores(tmp_path: Path) -> None:
    days = [date(2026, 8, d) for d in range(10, 18)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days, winner="T90")
    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "prereg" / "c1-m1-v0-min.yaml").read_text()
    )
    payload["prereg_id"] = "c1-m1-v0-min-synth"
    payload["walk_forward"]["min_train_days"] = 2
    payload["bootstrap"]["n_resample"] = 40
    payload["ridge_lambda_grid"] = [1.0]
    (tmp_path / "c1-m1-v0-min-synth.yaml").write_text(yaml.safe_dump(payload))
    report = run_c1_m1_v0_min(
        trades,  # type: ignore[arg-type]
        labels,
        prereg=payload,
        prereg_dir=tmp_path,
        ledger=Ledger(),
        n_resample=40,
    )
    assert report.is_strategy_pnl is False
    assert report.coverage.share >= 0.0
    assert report.n_predictions > 0
    assert report.decision.rule == "sign_test_oos_rps_improvement_vs_null"
    seasons = {row.key for row in report.by_season}
    assert "JJA" in seasons
    gbm = pytest.raises(ValueError, match="second experiment")
    if report.decision.verdict != "flow_carries_information":
        with gbm:
            residual_gbm_experiment(report)
    else:
        out = residual_gbm_experiment(report)
        assert out["is_strategy_pnl"] is False


def test_offset_mle_shrinks_toward_zero() -> None:
    # Two-class offset; large λ should keep β small.
    observations = [
        ([0.6, 0.4], [[1.0, 0.0], [0.0, 1.0]], 0),
        ([0.55, 0.45], [[1.0, 0.0], [0.0, 1.0]], 0),
        ([0.7, 0.3], [[0.5, 0.5], [0.2, 0.8]], 0),
    ]
    beta = fit_offset_logit_mle(observations, ridge_lambda=100.0, max_iter=80)
    assert all(abs(v) < 0.5 for v in beta)


def test_climatology_nearby_doy() -> None:
    days = [date(2025, 8, 12), date(2024, 8, 11), date(2026, 1, 1)]
    labels = _labels_for_days(days, winner="T80")
    tickers = [_ticker(date(2026, 8, 12), "T80"), _ticker(date(2026, 8, 12), "T90")]
    clim = climatology_forecast(
        days[:2], labels, date(2026, 8, 12), tickers, window=15
    )
    assert clim is not None
    assert clim[tickers[0]] == Decimal("1")
