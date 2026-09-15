"""C1-M1 v0: microstructure-only fair value. Scores, never P&L.

logit(p_hat) = logit(q_trade) + beta · x with x from flow and calendar only.
β = 0 reproduces the normalised trade-derived market ladder, so beating
the market is exactly β ≠ 0 out of sample. There is no second market
baseline.

This backtest delivers walk-forward ranked probability score with
day-clustered intervals. It does not deliver P&L. Quote placement, queue
position, fill probability and adverse selection live in B3's shadow-fill
comparator. Any P&L number at this stage is an artifact.

Stopping points (each can invalidate everything after it):
    1. outcome × book_side cross-tab
    2. two-sided coverage vs the pre-registered floor
    3. null recovery on the trade anchor
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    bootstrap_mean_ci,
    clustered_bootstrap_mean_ci,
    season_of,
)
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.backtest.ledger import Ledger, canonical_json, refuse_unless_preregistered
from wxmm.core.errors import NwpInterpolationUnspecified
from wxmm.eval.ratio import pairwise_ratio_table
from wxmm.eval.scores import brier_decomposition, log_score, ranked_probability_score
from wxmm.fairvalue.anchor import apply_logit_adjustment
from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping
from wxmm.fairvalue.coverage import (
    TradeAnchorCoverage,
    hours_to_close_bucket,
    mm_program_era,
    refuse_unless_coverage,
    staleness_bucket,
    trade_anchor_coverage,
)
from wxmm.fairvalue.model_trades import (
    _ridge,
    _row_features,
    null_trade_recovery,
    trade_ladder_or_none,
)
from wxmm.fairvalue.v0_baselines import (
    NBM_STATUS,
    climatology_forecast,
    nbm_forecast,
    persistence_forecast,
    sort_ladder,
)
from wxmm.fairvalue.walkforward import (
    DEFAULT_HOURS_TO_CLOSE,
    assert_schedule_integrity,
    expanding_origins,
)


@dataclass(frozen=True, slots=True)
class V0Prediction:
    climate_day: date
    as_of: datetime
    hours_to_close: int
    season: str
    era: str
    staleness_bucket: str
    realised: str
    p_hat: dict[str, Decimal]
    q_null: dict[str, Decimal]
    persistence: dict[str, Decimal] | None
    climatology: dict[str, Decimal] | None
    nbm_status: Literal["UNAVAILABLE_INTERPOLATION_UNSPECIFIED"]
    rps_model: Decimal
    rps_null: Decimal
    rps_persistence: Decimal | None
    rps_climatology: Decimal | None
    log_model: Decimal
    log_null: Decimal


@dataclass(frozen=True, slots=True)
class BetaInterval:
    name: str
    point: float
    ci_low: float | None
    ci_high: float | None


@dataclass(frozen=True, slots=True)
class SliceScore:
    key: str
    n: int
    mean_rps_improvement: float
    ci_low: float | None
    ci_high: float | None


@dataclass(frozen=True, slots=True)
class V0Decision:
    rule: Literal["sign_test_oos_rps_improvement_vs_null"]
    mean_improvement: float
    ci_low: float | None
    ci_high: float | None
    verdict: Literal["proceed_to_v1", "flow_adds_nothing", "harmful_check_sign", "ci_undefined"]


@dataclass(frozen=True, slots=True)
class V0Report:
    """Out-of-sample scores. Not P&L."""

    prereg_id: str
    coverage: TradeAnchorCoverage
    n_predictions: int
    mean_rps_improvement: float
    clustered_ci: tuple[float, float] | None
    contract_ci: tuple[float, float] | None
    decision: V0Decision
    by_season: tuple[SliceScore, ...]
    by_hours_to_close: tuple[SliceScore, ...]
    by_staleness: tuple[SliceScore, ...]
    by_era_jja: tuple[SliceScore, ...]
    beta: tuple[BetaInterval, ...]
    brier_model_reconciles: bool
    brier_null_reconciles: bool
    unc_common_within_1pct: bool
    nbm_status: Literal["UNAVAILABLE_INTERPOLATION_UNSPECIFIED"]
    is_strategy_pnl: Literal[False] = False

    def as_dict(self) -> dict[str, object]:
        return {
            "prereg_id": self.prereg_id,
            "coverage": self.coverage.as_dict(),
            "n_predictions": self.n_predictions,
            "mean_rps_improvement": self.mean_rps_improvement,
            "clustered_ci": self.clustered_ci,
            "contract_ci": self.contract_ci,
            "decision": asdict(self.decision),
            "by_season": [asdict(row) for row in self.by_season],
            "by_hours_to_close": [asdict(row) for row in self.by_hours_to_close],
            "by_staleness": [asdict(row) for row in self.by_staleness],
            "by_era_jja": [asdict(row) for row in self.by_era_jja],
            "beta": [asdict(row) for row in self.beta],
            "brier_model_reconciles": self.brier_model_reconciles,
            "brier_null_reconciles": self.brier_null_reconciles,
            "unc_common_within_1pct": self.unc_common_within_1pct,
            "nbm_status": self.nbm_status,
            "is_strategy_pnl": False,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.as_dict())


def _tickers_on(labels: Mapping[str, SettlementLabel], climate_day: date) -> list[str]:
    return sort_ladder(
        [ticker for ticker, label in labels.items() if label.climate_day == climate_day]
    )


def _realised(labels: Mapping[str, SettlementLabel], climate_day: date) -> str | None:
    won = [
        ticker
        for ticker, label in labels.items()
        if label.climate_day == climate_day and label.yes_won
    ]
    if len(won) != 1:
        return None
    return won[0]


def _yes_won_map(labels: Mapping[str, SettlementLabel], tickers: Sequence[str]) -> dict[str, bool]:
    return {ticker: labels[ticker].yes_won for ticker in tickers if ticker in labels}


def _fit_beta(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    train_days: Sequence[date],
    *,
    hours_to_close: Sequence[int],
    ridge_lambda: float,
    mapping: Any,
) -> tuple[tuple[str, ...], tuple[float, ...]] | None:
    from wxmm.settlement.eras import kalshi_last_trading_close_utc

    names: list[str] | None = None
    x_rows: list[list[float]] = []
    y: list[float] = []
    for climate in train_days:
        tickers = _tickers_on(labels, climate)
        if len(tickers) < 2:
            continue
        yes_won = _yes_won_map(labels, tickers)
        close = kalshi_last_trading_close_utc(climate)
        for hours in hours_to_close:
            as_of = close - timedelta(hours=int(hours))
            ladder = trade_ladder_or_none(trades, tickers, as_of=as_of, mapping=mapping)
            if ladder is None:
                continue
            q = null_trade_recovery(ladder)
            for ticker in tickers:
                if ticker not in yes_won:
                    continue
                mid = float(q[ticker])
                residual = (1.0 if yes_won[ticker] else 0.0) - mid
                working = residual / max(mid * (1.0 - mid), 1e-6)
                feats = _row_features(trades, ticker, as_of, mapping=mapping)
                if names is None:
                    names = sorted(feats)
                x_rows.append([feats[name] for name in names])
                y.append(working)
    if names is None or not x_rows:
        return None
    return tuple(names), tuple(_ridge(x_rows, y, ridge_lambda))


def _predict_adjusted(
    trades: Sequence[RawTrade],
    tickers: Sequence[str],
    as_of: datetime,
    *,
    mapping: Any,
    names: Sequence[str],
    beta: Sequence[float],
) -> dict[str, Decimal] | None:
    ladder = trade_ladder_or_none(trades, tickers, as_of=as_of, mapping=mapping)
    if ladder is None:
        return None
    q = null_trade_recovery(ladder)
    adjs: dict[str, Decimal] = {}
    for ticker in tickers:
        feats = _row_features(trades, ticker, as_of, mapping=mapping)
        adj = sum(float(beta[i]) * float(feats[names[i]]) for i in range(len(names)))
        adjs[ticker] = Decimal(str(adj))
    return apply_logit_adjustment(q, adjs)


def _improvements(rows: Sequence[V0Prediction]) -> list[Decimal]:
    return [row.rps_null - row.rps_model for row in rows]


def _slice_scores(
    rows: Sequence[V0Prediction],
    key_of: Callable[[V0Prediction], str],
    *,
    seed: int,
    n_resample: int,
) -> tuple[SliceScore, ...]:
    grouped: dict[str, list[V0Prediction]] = {}
    for row in rows:
        grouped.setdefault(key_of(row), []).append(row)
    out: list[SliceScore] = []
    for key in sorted(grouped):
        items = grouped[key]
        values = _improvements(items)
        by_day: dict[date, list[Decimal]] = {}
        for row in items:
            by_day.setdefault(row.climate_day, []).append(row.rps_null - row.rps_model)
        ci = clustered_bootstrap_mean_ci(by_day, seed=seed, n_resample=n_resample)
        mean = float(sum(values) / len(values)) if values else 0.0
        out.append(
            SliceScore(
                key=key,
                n=len(items),
                mean_rps_improvement=mean,
                ci_low=float(ci[0]) if ci else None,
                ci_high=float(ci[1]) if ci else None,
            )
        )
    return tuple(out)


def _verdict(mean: float, ci: tuple[float, float] | None) -> V0Decision:
    if ci is None:
        return V0Decision(
            rule="sign_test_oos_rps_improvement_vs_null",
            mean_improvement=mean,
            ci_low=None,
            ci_high=None,
            verdict="ci_undefined",
        )
    lo, hi = ci
    if lo > 0:
        verdict: Literal["proceed_to_v1", "flow_adds_nothing", "harmful_check_sign"] = (
            "proceed_to_v1"
        )
    elif hi < 0:
        verdict = "harmful_check_sign"
    else:
        verdict = "flow_adds_nothing"
    return V0Decision(
        rule="sign_test_oos_rps_improvement_vs_null",
        mean_improvement=mean,
        ci_low=lo,
        ci_high=hi,
        verdict=verdict,
    )


def _beta_intervals(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    train_days: Sequence[date],
    *,
    hours_to_close: Sequence[int],
    ridge_lambda: float,
    mapping: Any,
    seed: int,
    n_resample: int,
) -> tuple[BetaInterval, ...]:
    fitted = _fit_beta(
        trades,
        labels,
        train_days,
        hours_to_close=hours_to_close,
        ridge_lambda=ridge_lambda,
        mapping=mapping,
    )
    if fitted is None:
        return ()
    names, point = fitted
    days = list(train_days)
    if len(days) < 2:
        return tuple(
            BetaInterval(name=name, point=point[i], ci_low=None, ci_high=None)
            for i, name in enumerate(names)
        )
    import random

    rng = random.Random(seed)
    draws: list[list[float]] = [[] for _ in names]
    for _ in range(n_resample):
        sample_days = [days[rng.randrange(len(days))] for _ in days]
        refit = _fit_beta(
            trades,
            labels,
            sample_days,
            hours_to_close=hours_to_close,
            ridge_lambda=ridge_lambda,
            mapping=mapping,
        )
        if refit is None or refit[0] != names:
            continue
        for i, value in enumerate(refit[1]):
            draws[i].append(value)
    out: list[BetaInterval] = []
    for i, name in enumerate(names):
        series = [Decimal(str(v)) for v in draws[i]]
        ci = bootstrap_mean_ci(series, seed=seed + i, n_resample=min(n_resample, 200))
        out.append(
            BetaInterval(
                name=name,
                point=point[i],
                ci_low=float(ci[0]) if ci else None,
                ci_high=float(ci[1]) if ci else None,
            )
        )
    return tuple(out)


def run_c1_m1_v0(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    prereg: Mapping[str, Any],
    prereg_dir: Path,
    ledger: Ledger,
    n_resample: int | None = None,
) -> V0Report:
    """Cross-tab → coverage → null recovery → walk-forward scores.

    Refuses to fit when coverage is below the pre-registered floor.
    Never returns a currency amount.
    """
    registered = refuse_unless_preregistered(dict(prereg), prereg_dir)
    mapping = assert_outcome_bookside_mapping(
        [trade for trade in trades if not trade.is_block_trade]
    )
    floor = float(registered.get("coverage_floor", 0.15))
    coverage = trade_anchor_coverage(trades, mapping=mapping, floor=floor)
    refuse_unless_coverage(coverage, floor=floor)

    hours = tuple(int(h) for h in registered.get("hours_to_close") or DEFAULT_HOURS_TO_CLOSE)
    min_train = int(registered.get("walk_forward", {}).get("min_train_days") or 2)
    ridge = float(registered.get("ridge_lambda", 1.0))
    seed = int(registered.get("seed", 0))
    boot_n = int(n_resample or registered.get("bootstrap", {}).get("n_resample") or 200)

    climate_days = sorted({label.climate_day for label in labels.values()})
    origins = list(expanding_origins(climate_days, min_train_days=min_train, hours_to_close=hours))
    assert_schedule_integrity(origins)

    predictions: list[V0Prediction] = []
    last_names: tuple[str, ...] = ()
    last_beta: tuple[float, ...] = ()
    for origin in origins:
        fitted = _fit_beta(
            trades,
            labels,
            origin.train_days,
            hours_to_close=hours,
            ridge_lambda=ridge,
            mapping=mapping,
        )
        if fitted is None:
            continue
        last_names, last_beta = fitted
        tickers = _tickers_on(labels, origin.predict_day)
        won_ticker = _realised(labels, origin.predict_day)
        if won_ticker is None or len(tickers) < 2:
            continue
        ladder = trade_ladder_or_none(trades, tickers, as_of=origin.as_of, mapping=mapping)
        if ladder is None:
            continue
        q_null = null_trade_recovery(ladder)
        p_hat = _predict_adjusted(
            trades,
            tickers,
            origin.as_of,
            mapping=mapping,
            names=last_names,
            beta=last_beta,
        )
        if p_hat is None:
            continue
        persist = persistence_forecast(
            [t for t in trades if t.created_time <= origin.as_of], tickers
        )
        clima = climatology_forecast(
            labels,
            tickers,
            predict_day=origin.predict_day,
            train_days=origin.train_days,
            window_days=int(registered.get("climatology_doy_window", 15)),
        )
        try:
            nbm_forecast()
            nbm_status = NBM_STATUS
        except NwpInterpolationUnspecified:
            nbm_status = NBM_STATUS
        ages: list[float] = []
        for book in ladder.books.values():
            if book.bid_staleness is not None:
                ages.append(book.bid_staleness.total_seconds())
            if book.ask_staleness is not None:
                ages.append(book.ask_staleness.total_seconds())
        stale = max(ages) if ages else None
        predictions.append(
            V0Prediction(
                climate_day=origin.predict_day,
                as_of=origin.as_of,
                hours_to_close=origin.hours_to_close,
                season=season_of(origin.predict_day),
                era=mm_program_era(origin.predict_day),
                staleness_bucket=staleness_bucket(stale),
                realised=won_ticker,
                p_hat=p_hat,
                q_null=q_null,
                persistence=persist,
                climatology=clima,
                nbm_status=nbm_status,
                rps_model=ranked_probability_score(p_hat, won_ticker),
                rps_null=ranked_probability_score(q_null, won_ticker),
                rps_persistence=(
                    ranked_probability_score(persist, won_ticker) if persist else None
                ),
                rps_climatology=(ranked_probability_score(clima, won_ticker) if clima else None),
                log_model=log_score(p_hat, won_ticker),
                log_null=log_score(q_null, won_ticker),
            )
        )

    if not predictions:
        raise ValueError("no complete walk-forward prediction rows after coverage passed")

    deltas = _improvements(predictions)
    mean_imp = float(sum(deltas) / len(deltas))
    by_day: dict[date, list[Decimal]] = {}
    for row in predictions:
        by_day.setdefault(row.climate_day, []).append(row.rps_null - row.rps_model)
    clustered = clustered_bootstrap_mean_ci(by_day, seed=seed, n_resample=boot_n)
    contract = bootstrap_mean_ci(deltas, seed=seed, n_resample=boot_n)
    clustered_t = (float(clustered[0]), float(clustered[1])) if clustered else None
    contract_t = (float(contract[0]), float(contract[1])) if contract else None

    realised_ids = [row.realised for row in predictions]
    brier_m = brier_decomposition([row.p_hat for row in predictions], realised_ids)
    brier_n = brier_decomposition([row.q_null for row in predictions], realised_ids)
    unc_ok = abs(float(brier_m.uncertainty) - float(brier_n.uncertainty)) <= 0.01 * max(
        abs(float(brier_m.uncertainty)), abs(float(brier_n.uncertainty)), 1e-9
    )

    def _by_position(
        forecast: Mapping[str, Decimal], realised: str
    ) -> tuple[dict[str, Decimal], str]:
        ordered = sort_ladder(list(forecast))
        keyed = {str(i): forecast[ticker] for i, ticker in enumerate(ordered)}
        return keyed, str(ordered.index(realised))

    positioned = [_by_position(row.p_hat, row.realised) for row in predictions]
    _ = pairwise_ratio_table([item[0] for item in positioned], [item[1] for item in positioned])

    beta = _beta_intervals(
        trades,
        labels,
        origins[-1].train_days if origins else climate_days[:-1],
        hours_to_close=hours,
        ridge_lambda=ridge,
        mapping=mapping,
        seed=seed,
        n_resample=min(boot_n, 80),
    )
    jja = [row for row in predictions if row.season == "JJA"]
    report = V0Report(
        prereg_id=str(registered["prereg_id"]),
        coverage=coverage,
        n_predictions=len(predictions),
        mean_rps_improvement=mean_imp,
        clustered_ci=clustered_t,
        contract_ci=contract_t,
        decision=_verdict(mean_imp, clustered_t),
        by_season=_slice_scores(predictions, lambda r: r.season, seed=seed, n_resample=boot_n),
        by_hours_to_close=_slice_scores(
            predictions,
            lambda r: hours_to_close_bucket(float(r.hours_to_close)),
            seed=seed,
            n_resample=boot_n,
        ),
        by_staleness=_slice_scores(
            predictions, lambda r: r.staleness_bucket, seed=seed, n_resample=boot_n
        ),
        by_era_jja=_slice_scores(jja, lambda r: r.era, seed=seed, n_resample=boot_n),
        beta=beta,
        brier_model_reconciles=brier_m.reconcile(),
        brier_null_reconciles=brier_n.reconcile(),
        unc_common_within_1pct=unc_ok,
        nbm_status=NBM_STATUS,
    )
    ledger.record(
        "C1_M1_V0",
        {
            "prereg_id": report.prereg_id,
            "n_predictions": report.n_predictions,
            "decision": report.decision.verdict,
            "two_sided_share": coverage.two_sided_share,
            "nbm_status": NBM_STATUS,
            "is_strategy_pnl": False,
        },
    )
    return report


def predict_only_null_recovery(ladder: Any) -> dict[str, Decimal]:
    """Public null: β = 0. Used by the mutation test."""
    return null_trade_recovery(ladder)
