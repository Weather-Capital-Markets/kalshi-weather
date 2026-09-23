"""C1-M1 v0-MINIMAL: walk-forward, MLE null recovery, clustered CI, coverage."""

from __future__ import annotations

import random
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
from wxmm.fairvalue.anchor_trades import (
    assert_outcome_bookside_mapping,
    refuse_if_leaked,
)
from wxmm.fairvalue.features import WINDOW_INVARIANT_KEYS
from wxmm.fairvalue.model_trades import (
    GAP_SINCE_LAST_DECISION,
    ImputationTally,
    _row_features,
    null_trade_recovery,
    trade_ladder_or_none,
)
from wxmm.fairvalue.v0_min import (
    assert_design_is_identified,
    climatology_forecast,
    fit_offset_logit_mle,
    percentile_ci,
    predict_adjusted,
    reliability_by_ladder_position,
    residual_gbm_experiment,
    run_c1_m1_v0_min,
    trade_anchor_coverage,
    trade_anchor_coverage_report,
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


def test_coverage_report_slices_and_staleness() -> None:
    days = [date(2026, 8, d) for d in range(10, 16)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days)
    report = trade_anchor_coverage_report(
        trades,  # type: ignore[arg-type]
        labels,
        hours_to_close=(24, 12),
        mapping=None,
    )
    assert report.pooled.n_grid == len(days) * 2
    assert report.pooled.share == 1.0
    season_keys = {row.key for row in report.by_season}
    assert "JJA" in season_keys
    hours_keys = {row.key for row in report.by_hours_to_close}
    assert hours_keys == {"12", "24"}
    bracket_keys = {row.key for row in report.by_bracket_position}
    assert bracket_keys == {"T80", "T90"}
    for row in report.by_season:
        assert 0.0 <= row.share <= 1.0
        assert row.n_both_sides_uncrossed <= row.n_grid
    assert set(report.staleness) == {
        "p50_bid_staleness_seconds",
        "p90_bid_staleness_seconds",
        "p50_ask_staleness_seconds",
        "p90_ask_staleness_seconds",
    }
    assert all(v is None or v >= 0.0 for v in report.staleness.values())


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
    block = report.as_dict()["imputation"]
    assert isinstance(block, dict)
    assert block["gap_since_last_decision"] == GAP_SINCE_LAST_DECISION
    assert block["rows_excluded_missing_staleness"] == 0
    imputed = block["imputed_cells"]
    assert isinstance(imputed, dict)
    assert any(key.endswith("gap_since_last_seconds") for key in imputed)
    assert not any("staleness" in key for key in imputed)
    reliability_rows = report.as_dict()["reliability_by_position"]
    assert isinstance(reliability_rows, list) and reliability_rows
    assert all(isinstance(row, dict) and row["n"] > 0 for row in reliability_rows)
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


def _moving_tape(day: date) -> list[object]:
    """Both sides re-printed inside each window, at a spread that keeps moving.

    Close is 04:59 the next day, so prints have to sit in the final hours or the
    15m/1h/4h lags all land on the same book and every change reads zero.
    """
    ticker = _ticker(day, "T80")
    quotes = [(18, 0, "0.50", "0.44"), (21, 0, "0.52", "0.50"),
              (22, 0, "0.46", "0.36"), (22, 50, "0.55", "0.51")]
    out: list[object] = []
    for hour, minute, ask, bid in quotes:
        created = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        tag = f"{hour:02d}{minute:02d}"
        out.append(
            _raw(trade_id=f"{ticker}-{tag}y", ticker=ticker, outcome="yes",
                 book="ask", yes=ask, no=f"{1 - float(ask):.2f}", created=created)
        )
        out.append(
            _raw(trade_id=f"{ticker}-{tag}n", ticker=ticker, outcome="no",
                 book="bid", yes=bid, no=f"{1 - float(bid):.2f}", created=created)
        )
    return out


def test_window_invariant_asof_features_emitted_once() -> None:
    """Staleness and spread read the as-of trade state, which no window touches.

    Emitting them per window put three identical columns in the design. Ridge
    does not complain, it just hands each copy a third of the effect.
    """
    day = date(2026, 8, 12)
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=6)
    feats = _row_features(
        _moving_tape(day),  # type: ignore[arg-type]
        _ticker(day, "T80"),
        as_of,
        mapping=None,
    )
    assert feats is not None
    for key in WINDOW_INVARIANT_KEYS:
        assert f"asof_{key}" in feats
        assert not [name for name in feats if name.endswith(f"_{key}") and name.startswith("w")]
    assert len([n for n in feats if n.endswith("_signed_ofi")]) == 3
    assert "w900s_trade_count" not in feats
    assert "w900s_intensity_per_hour" in feats


def test_implied_spread_change_is_measured_over_the_window() -> None:
    """The spec asks for the spread *and its change*. A never-populated column
    standardises to a constant and silently contributes nothing."""
    day = date(2026, 8, 12)
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=6)
    feats = _row_features(
        _moving_tape(day),  # type: ignore[arg-type]
        _ticker(day, "T80"),
        as_of,
        mapping=None,
    )
    assert feats is not None
    changes = {n: v for n, v in feats.items() if n.endswith("_implied_spread_change")}
    assert len(changes) == 3
    assert any(abs(v) > 1e-9 for v in changes.values())


def test_beta_interval_is_a_percentile_not_a_ci_on_the_bootstrap_mean() -> None:
    """The draws are refits of one coefficient, so the interval must bracket the
    coefficient. Bracketing the mean of the draws gives something that narrows
    with the refit count and can exclude the point estimate it is reported next
    to, which reads as significance that is not there."""
    rng = random.Random(3)
    draws = [rng.gauss(0.0, 1.0) for _ in range(400)]
    ci = percentile_ci(draws)
    assert ci is not None
    assert ci[0] < -1.0 and ci[1] > 1.0
    mean_ci = bootstrap_mean_ci([Decimal(str(v)) for v in draws], seed=0, n_resample=400)
    assert mean_ci is not None
    assert (ci[1] - ci[0]) > 10 * float(mean_ci[1] - mean_ci[0])
    assert percentile_ci([0.5]) is None


def test_reported_beta_intervals_contain_their_point() -> None:
    days = [date(2026, 8, d) for d in range(10, 20)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days, winner="T90")
    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "prereg" / "c1-m1-v0-min.yaml").read_text()
    )
    payload["prereg_id"] = "c1-m1-v0-min-beta"
    payload["walk_forward"]["min_train_days"] = 2
    payload["ridge_lambda_grid"] = [1.0]
    tmp = Path(__file__).resolve().parent / "_beta_prereg"
    tmp.mkdir(exist_ok=True)
    (tmp / "c1-m1-v0-min-beta.yaml").write_text(yaml.safe_dump(payload))
    try:
        report = run_c1_m1_v0_min(
            trades,  # type: ignore[arg-type]
            labels,
            prereg=payload,
            prereg_dir=tmp,
            ledger=Ledger(),
            n_resample=30,
        )
    finally:
        (tmp / "c1-m1-v0-min-beta.yaml").unlink()
        tmp.rmdir()
    assert report.beta
    for row in report.beta:
        if row.ci_low is None or row.ci_high is None:
            continue
        assert row.ci_low <= row.point <= row.ci_high, row.name


def test_collinear_design_is_refused() -> None:
    rng = random.Random(7)
    rows = [[rng.random(), rng.random()] for _ in range(200)]
    assert_design_is_identified(["a", "b"], rows)
    doubled = [[r[0], r[1], 2.0 * r[0]] for r in rows]
    with pytest.raises(ValueError, match="collinear"):
        assert_design_is_identified(["a", "b", "a_scaled"], doubled)


def test_climatology_nearby_doy() -> None:
    days = [date(2025, 8, 12), date(2024, 8, 11), date(2026, 1, 1)]
    labels = _labels_for_days(days, winner="T80")
    tickers = [_ticker(date(2026, 8, 12), "T80"), _ticker(date(2026, 8, 12), "T90")]
    clim = climatology_forecast(
        days[:2], labels, date(2026, 8, 12), tickers, window=15
    )
    assert clim is not None
    assert clim[tickers[0]] == Decimal("1")


def test_shard_coverage_does_not_multiply_grid() -> None:
    days = [date(2026, 8, d) for d in range(10, 16)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days)
    hours = (24, 12)
    full = trade_anchor_coverage(trades, labels, hours_to_close=hours, mapping=None)  # type: ignore[arg-type]
    first_days = set(days[:3])
    second_days = set(days[3:])
    first_trades = [t for t in trades if t.climate_day in first_days]  # type: ignore[attr-defined]
    second_trades = [t for t in trades if t.climate_day in second_days]  # type: ignore[attr-defined]
    first_labels = {k: v for k, v in labels.items() if v.climate_day in first_days}
    second_labels = {k: v for k, v in labels.items() if v.climate_day in second_days}
    part_a = trade_anchor_coverage(
        first_trades, first_labels, hours_to_close=hours, mapping=None  # type: ignore[arg-type]
    )
    part_b = trade_anchor_coverage(
        second_trades, second_labels, hours_to_close=hours, mapping=None  # type: ignore[arg-type]
    )
    assert part_a.n_grid + part_b.n_grid == full.n_grid
    assert (
        part_a.n_both_sides_uncrossed + part_b.n_both_sides_uncrossed
        == full.n_both_sides_uncrossed
    )


def test_cache_ingest_release_matches_in_memory(tmp_path: Path) -> None:
    days = [date(2026, 8, d) for d in range(10, 18)]
    trades = [t for day in days for t in _pair_for_day(day)]
    labels = _labels_for_days(days, winner="T90")
    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "prereg" / "c1-m1-v0-min.yaml").read_text()
    )
    payload["prereg_id"] = "c1-m1-v0-min-cache"
    payload["walk_forward"]["min_train_days"] = 2
    payload["bootstrap"]["n_resample"] = 20
    payload["ridge_lambda_grid"] = [1.0]
    (tmp_path / "c1-m1-v0-min-cache.yaml").write_text(yaml.safe_dump(payload))
    from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
    from wxmm.fairvalue.crossed import crossed_diagnostic
    from wxmm.fairvalue.v0_min import ObservationCache

    mapping = assert_outcome_bookside_mapping(trades)  # type: ignore[arg-type]
    crossed = crossed_diagnostic(trades)  # type: ignore[arg-type]
    hours = tuple(int(h) for h in payload["hours_to_close"])
    cache = ObservationCache([], labels, hours_to_close=hours, mapping=mapping)
    by_day: dict[date, list[object]] = {}
    for trade in trades:
        by_day.setdefault(trade.climate_day, []).append(trade)  # type: ignore[attr-defined]
    for climate, rows in by_day.items():
        cache.ingest(rows)  # type: ignore[arg-type]
        cache.warm([climate])
        cache.release_days([climate])
    coverage = trade_anchor_coverage(
        trades, labels, hours_to_close=hours, mapping=mapping  # type: ignore[arg-type]
    )
    packed = run_c1_m1_v0_min(
        None,
        labels,
        prereg=payload,
        prereg_dir=tmp_path,
        ledger=Ledger(),
        n_resample=20,
        mapping=mapping,
        crossed=crossed,
        cache=cache,
        coverage=coverage,
    )
    direct = run_c1_m1_v0_min(
        trades,  # type: ignore[arg-type]
        labels,
        prereg=payload,
        prereg_dir=tmp_path,
        ledger=Ledger(),
        n_resample=20,
    )
    assert packed.n_predictions == direct.n_predictions
    assert packed.mean_rps_improvement == direct.mean_rps_improvement
    assert packed.n_climate_day_clusters == direct.n_climate_day_clusters
    assert packed.coverage.n_grid == direct.coverage.n_grid
    assert cache.n_cached > 0


def test_reliability_table_is_predicted_versus_frequency_by_position() -> None:
    bottom = "KXHIGHNY-26JUL04-T83"
    middle = "KXHIGHNY-26JUL04-B85.5"
    forecasts = [
        {bottom: Decimal("0.25"), middle: Decimal("0.75")},
        {bottom: Decimal("0.75"), middle: Decimal("0.25")},
    ]
    table = reliability_by_ladder_position(forecasts, [bottom, middle])
    assert [row.position for row in table] == [0, 1]
    assert table[0].n == 2
    assert table[0].mean_predicted == pytest.approx(0.5)
    assert table[0].empirical_frequency == pytest.approx(0.5)
    assert table[1].mean_predicted == pytest.approx(0.5)
    assert table[1].empirical_frequency == pytest.approx(0.5)
    both_bottom = reliability_by_ladder_position(forecasts, [bottom, bottom])
    assert both_bottom[0].empirical_frequency == pytest.approx(1.0)
    assert both_bottom[0].mean_predicted == pytest.approx(0.5)
    assert both_bottom[1].empirical_frequency == pytest.approx(0.0)
    assert reliability_by_ladder_position([], []) == ()


def test_missing_staleness_is_excluded_not_imputed() -> None:
    """The scored ticker has no bid. A second ticker keeps the cross-tab clean."""
    day = date(2026, 8, 12)
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=6)
    created = (as_of - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scored = _ticker(day, "T80")
    other = _ticker(day, "T90")
    tape = [
        _raw(
            trade_id=f"{scored}-y",
            ticker=scored,
            outcome="yes",
            book="ask",
            yes="0.55",
            no="0.45",
            created=created,
        ),
        _raw(
            trade_id=f"{other}-y",
            ticker=other,
            outcome="yes",
            book="ask",
            yes="0.40",
            no="0.60",
            created=created,
        ),
        _raw(
            trade_id=f"{other}-n",
            ticker=other,
            outcome="no",
            book="bid",
            yes="0.30",
            no="0.70",
            created=created,
        ),
    ]
    mapping = assert_outcome_bookside_mapping(tape)  # type: ignore[arg-type]
    one_sided = [trade for trade in tape if trade.ticker == scored]  # type: ignore[attr-defined]
    tally = ImputationTally()
    feats = _row_features(
        one_sided,  # type: ignore[arg-type]
        scored,
        as_of,
        mapping=mapping,
        tally=tally,
    )
    assert feats is None
    assert tally.rows_excluded_missing_staleness == 1
    assert tally.imputed == {}
    with pytest.raises(AssertionError, match="staleness"):
        tally.note_imputed("bid_staleness_seconds")
    with pytest.raises(AssertionError, match="staleness"):
        tally.note_imputed("asof_staleness_ratio")


def test_gap_since_last_stays_imputed_and_is_counted() -> None:
    """Prints sit inside the 4h window and outside the 15m and 1h windows.

    Both sides are present, so staleness is observed. The empty windows still
    fill gap_since_last_seconds with 0.0; that fill is counted, not removed.
    """
    day = date(2026, 8, 12)
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=6)
    created = (as_of - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ticker = _ticker(day, "T80")
    tape = [
        _raw(
            trade_id=f"{ticker}-y",
            ticker=ticker,
            outcome="yes",
            book="ask",
            yes="0.55",
            no="0.45",
            created=created,
        ),
        _raw(
            trade_id=f"{ticker}-n",
            ticker=ticker,
            outcome="no",
            book="bid",
            yes="0.45",
            no="0.55",
            created=created,
        ),
    ]
    tally = ImputationTally()
    feats = _row_features(tape, ticker, as_of, mapping=None, tally=tally)  # type: ignore[arg-type]
    assert feats is not None
    assert tally.rows_excluded_missing_staleness == 0
    assert tally.as_dict()["gap_since_last_decision"] == GAP_SINCE_LAST_DECISION
    assert tally.imputed.get("w900s_gap_since_last_seconds") == 1
    assert tally.imputed.get("w3600s_gap_since_last_seconds") == 1
    assert "w14400s_gap_since_last_seconds" not in tally.imputed
    assert feats["w900s_gap_since_last_seconds"] == 0.0
    assert not any("staleness" in key for key in tally.imputed)
