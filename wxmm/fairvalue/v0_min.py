"""C1-M1 v0-MINIMAL: offset-logit ridge MLE, walk-forward RPS, never P&L.

logit(p_hat) = logit(q_trade) + β·x with x from flow and calendar only.
β = 0 reproduces the normalised trade-derived market ladder.

Coverage is reported, not a halt. Residual GBM is the second experiment and
refuses unless the clustered sign test says flow carries information.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    bootstrap_mean_ci,
    clustered_bootstrap_mean_ci,
    season_of,
)
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.backtest.ledger import Ledger, canonical_json, refuse_unless_preregistered
from wxmm.core.errors import InconsistentTakerMapping
from wxmm.eval.scores import ranked_probability_score
from wxmm.fairvalue.anchor import apply_logit_adjustment
from wxmm.fairvalue.anchor_trades import (
    ObservedMapping,
    assert_outcome_bookside_mapping,
    filter_trades_as_of,
)
from wxmm.fairvalue.crossed import CrossedDiagnostic, crossed_diagnostic
from wxmm.fairvalue.model_trades import (
    _row_features,
    null_trade_recovery,
    trade_ladder_or_none,
)
from wxmm.fairvalue.walkforward import (
    DEFAULT_HOURS_TO_CLOSE,
    assert_schedule_integrity,
    expanding_origins,
)
from wxmm.settlement.eras import kalshi_last_trading_close_utc

RIDGE_LAMBDA_GRID: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0)
_CLIP = 20.0


@dataclass(frozen=True, slots=True)
class OffsetLogitFit:
    feature_names: tuple[str, ...]
    beta: tuple[float, ...]
    ridge_lambda: float
    feat_mean: tuple[float, ...]
    feat_std: tuple[float, ...]
    n_days: int
    is_strategy_pnl: Literal[False] = False


@dataclass(frozen=True, slots=True)
class V0MinPrediction:
    climate_day: date
    as_of: datetime
    hours_to_close: int
    season: str
    realised: str
    p_hat: dict[str, Decimal]
    q_null: dict[str, Decimal]
    climatology: dict[str, Decimal] | None
    rps_model: Decimal
    rps_null: Decimal
    rps_climatology: Decimal | None


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
class V0MinDecision:
    rule: Literal["sign_test_oos_rps_improvement_vs_null"]
    mean_improvement: float
    ci_low: float | None
    ci_high: float | None
    verdict: Literal[
        "flow_carries_information",
        "flow_adds_nothing",
        "harmful_check_sign",
        "ci_undefined",
    ]


@dataclass(frozen=True, slots=True)
class AnchorCoverage:
    n_grid: int
    n_both_sides_uncrossed: int
    share: float


@dataclass(frozen=True, slots=True)
class V0MinReport:
    """Out-of-sample scores. Not P&L."""

    prereg_id: str
    crossed: CrossedDiagnostic
    coverage: AnchorCoverage
    n_predictions: int
    mean_rps_improvement: float
    clustered_ci: tuple[float, float] | None
    contract_ci: tuple[float, float] | None
    decision: V0MinDecision
    by_season: tuple[SliceScore, ...]
    beta: tuple[BetaInterval, ...]
    nbm_status: Literal["NOT_IN_V0_MIN"] = "NOT_IN_V0_MIN"
    is_strategy_pnl: Literal[False] = False

    def as_dict(self) -> dict[str, object]:
        return {
            "prereg_id": self.prereg_id,
            "crossed_rate": asdict(self.crossed.as_specified),
            "crossed_rate_inverted": asdict(self.crossed.inverted),
            "crossed_verdict": self.crossed.verdict,
            "coverage": asdict(self.coverage),
            "n_predictions": self.n_predictions,
            "mean_rps_improvement": self.mean_rps_improvement,
            "clustered_ci": self.clustered_ci,
            "contract_ci": self.contract_ci,
            "decision": asdict(self.decision),
            "by_season": [asdict(row) for row in self.by_season],
            "beta": [asdict(row) for row in self.beta],
            "nbm_status": self.nbm_status,
            "is_strategy_pnl": False,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.as_dict())


def sort_ladder(tickers: Sequence[str]) -> list[str]:
    return sorted(tickers)


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


def _yes_won_map(
    labels: Mapping[str, SettlementLabel], tickers: Sequence[str]
) -> dict[str, bool]:
    return {ticker: labels[ticker].yes_won for ticker in tickers if ticker in labels}


def _logit(p: float) -> float:
    clipped = min(max(p, 1e-9), 1.0 - 1e-9)
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(z: float) -> float:
    zc = max(-_CLIP, min(_CLIP, z))
    return 1.0 / (1.0 + math.exp(-zc))


def _standardize(
    rows: list[list[float]],
    mean: Sequence[float] | None = None,
    std: Sequence[float] | None = None,
) -> tuple[list[list[float]], tuple[float, ...], tuple[float, ...]]:
    if not rows:
        return [], (), ()
    k = len(rows[0])
    if mean is None or std is None:
        means = [sum(row[j] for row in rows) / len(rows) for j in range(k)]
        vars_ = [
            sum((row[j] - means[j]) ** 2 for row in rows) / max(len(rows), 1)
            for j in range(k)
        ]
        stds = [math.sqrt(v) if v > 1e-12 else 1.0 for v in vars_]
    else:
        means = list(mean)
        stds = [s if s > 1e-12 else 1.0 for s in std]
    scaled = [
        [(row[j] - means[j]) / stds[j] for j in range(k)] for row in rows
    ]
    return scaled, tuple(means), tuple(stds)


def _offset_probs(
    q: Sequence[float],
    x_rows: Sequence[Sequence[float]],
    beta: Sequence[float],
) -> tuple[list[float], list[float]]:
    unnorm: list[float] = []
    for qi, xi in zip(q, x_rows, strict=True):
        adj = sum(b * v for b, v in zip(beta, xi, strict=True))
        unnorm.append(_sigmoid(_logit(qi) + adj))
    total = sum(unnorm)
    if total <= 1e-18:
        n = len(unnorm)
        return [1.0 / n] * n, unnorm
    return [u / total for u in unnorm], unnorm


def _nll_and_grad(
    observations: Sequence[tuple[list[float], list[list[float]], int]],
    beta: list[float],
    lam: float,
) -> tuple[float, list[float]]:
    nll = 0.5 * lam * sum(b * b for b in beta)
    grad = [lam * b for b in beta]
    for q, x_rows, y_idx in observations:
        probs, unnorm = _offset_probs(q, x_rows, beta)
        py = max(probs[y_idx], 1e-12)
        nll -= math.log(py)
        x_y = x_rows[y_idx]
        s_y = unnorm[y_idx]
        for j, xj in enumerate(x_y):
            grad[j] -= (1.0 - s_y) * xj
        for pk, sk, xk in zip(probs, unnorm, x_rows, strict=True):
            factor = pk * (1.0 - sk)
            for j, xj in enumerate(xk):
                grad[j] += factor * xj
    return nll, grad


def fit_offset_logit_mle(
    observations: Sequence[tuple[list[float], list[list[float]], int]],
    *,
    ridge_lambda: float,
    max_iter: int = 200,
) -> list[float]:
    """Ridge-penalised multinomial MLE with trade-mid logit offset."""
    if not observations:
        return []
    k = len(observations[0][1][0])
    beta = [0.0] * k
    step = 0.25
    last = float("inf")
    for _ in range(max_iter):
        nll, grad = _nll_and_grad(observations, beta, ridge_lambda)
        gnorm = math.sqrt(sum(g * g for g in grad))
        if gnorm < 1e-8 or abs(last - nll) < 1e-10:
            break
        # Armijo backtracking
        trial_step = step
        improved = False
        for _bt in range(8):
            cand = [b - trial_step * g for b, g in zip(beta, grad, strict=True)]
            cand_nll, _ = _nll_and_grad(observations, cand, ridge_lambda)
            if cand_nll < nll:
                beta = cand
                last = cand_nll
                improved = True
                break
            trial_step *= 0.5
        if not improved:
            break
        if nll < last:
            last = nll
    return beta


def _day_observation(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    climate: date,
    as_of: datetime,
    *,
    mapping: ObservedMapping | None,
) -> tuple[list[str], list[float], list[list[float]], int, dict[str, Decimal]] | None:
    tickers = _tickers_on(labels, climate)
    realised = _realised(labels, climate)
    if realised is None or realised not in tickers or len(tickers) < 2:
        return None
    as_of_trades = filter_trades_as_of(trades, as_of)
    ladder = trade_ladder_or_none(
        as_of_trades, tickers, as_of=as_of, mapping=mapping
    )
    if ladder is None:
        return None
    q_map = null_trade_recovery(ladder)
    q = [float(q_map[t]) for t in tickers]
    names: list[str] | None = None
    x_rows: list[list[float]] = []
    for ticker in tickers:
        feats = _row_features(as_of_trades, ticker, as_of, mapping=mapping)
        if names is None:
            names = sorted(feats)
        x_rows.append([feats[name] for name in names])
    y_idx = tickers.index(realised)
    return tickers, q, x_rows, y_idx, q_map


def _collect_observations(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    days: Sequence[date],
    *,
    hours_to_close: Sequence[int],
    mapping: ObservedMapping | None,
) -> tuple[list[str] | None, list[tuple[list[float], list[list[float]], int]]]:
    names: list[str] | None = None
    out: list[tuple[list[float], list[list[float]], int]] = []
    for climate in days:
        close = kalshi_last_trading_close_utc(climate)
        for hours in hours_to_close:
            as_of = close - timedelta(hours=int(hours))
            packed = _day_observation(
                trades, labels, climate, as_of, mapping=mapping
            )
            if packed is None:
                continue
            _tickers, q, x_rows, y_idx, _qmap = packed
            if names is None:
                # feature names recovered from first row via _row_features order
                feats = _row_features(
                    filter_trades_as_of(trades, as_of),
                    _tickers[0],
                    as_of,
                    mapping=mapping,
                )
                names = sorted(feats)
            out.append((q, x_rows, y_idx))
    return names, out


def select_ridge_lambda(
    fit_obs: Sequence[tuple[list[float], list[list[float]], int]],
    sel_obs: Sequence[tuple[list[float], list[list[float]], int]],
    grid: Sequence[float],
    *,
    feat_mean: Sequence[float],
    feat_std: Sequence[float],
) -> float:
    if not fit_obs:
        return float(grid[0]) if grid else 1.0
    if not sel_obs:
        return 1.0 if 1.0 in grid else float(grid[0])
    best_lam = float(grid[0])
    best_nll = float("inf")
    for lam in grid:
        beta = fit_offset_logit_mle(fit_obs, ridge_lambda=float(lam))
        nll, _ = _nll_and_grad(list(sel_obs), beta, 0.0)
        if nll < best_nll:
            best_nll = nll
            best_lam = float(lam)
    _ = feat_mean, feat_std
    return best_lam


def predict_adjusted(
    q_map: Mapping[str, Decimal],
    tickers: Sequence[str],
    x_rows: Sequence[Sequence[float]],
    beta: Sequence[float],
) -> dict[str, Decimal]:
    adjs: dict[str, Decimal] = {}
    for ticker, xi in zip(tickers, x_rows, strict=True):
        adj = sum(float(b) * float(v) for b, v in zip(beta, xi, strict=True))
        adjs[ticker] = Decimal(str(adj))
    return apply_logit_adjustment(q_map, adjs)


def _doy_dist(a: int, b: int) -> int:
    raw = abs(a - b)
    return min(raw, 365 - raw)


def climatology_forecast(
    train_days: Sequence[date],
    labels: Mapping[str, SettlementLabel],
    predict_day: date,
    tickers: Sequence[str],
    *,
    window: int = 15,
) -> dict[str, Decimal] | None:
    """Empirical win frequency of ordered brackets on nearby LST day-of-year."""
    doy = predict_day.timetuple().tm_yday
    wins = [0] * len(tickers)
    n = 0
    for day in train_days:
        if _doy_dist(doy, day.timetuple().tm_yday) > window:
            continue
        day_tickers = _tickers_on(labels, day)
        if len(day_tickers) != len(tickers):
            continue
        won = _realised(labels, day)
        if won is None or won not in day_tickers:
            continue
        wins[day_tickers.index(won)] += 1
        n += 1
    if n == 0:
        return None
    return {tickers[i]: Decimal(wins[i]) / Decimal(n) for i in range(len(tickers))}


def trade_anchor_coverage(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    hours_to_close: Sequence[int],
    mapping: ObservedMapping | None,
) -> AnchorCoverage:
    days = sorted({label.climate_day for label in labels.values()})
    n_grid = 0
    n_ok = 0
    for climate in days:
        tickers = _tickers_on(labels, climate)
        if len(tickers) < 2:
            continue
        close = kalshi_last_trading_close_utc(climate)
        for hours in hours_to_close:
            as_of = close - timedelta(hours=int(hours))
            n_grid += 1
            as_of_trades = filter_trades_as_of(trades, as_of)
            ladder = trade_ladder_or_none(
                as_of_trades, tickers, as_of=as_of, mapping=mapping
            )
            if ladder is not None:
                n_ok += 1
    share = float(n_ok) / float(n_grid) if n_grid else 0.0
    return AnchorCoverage(n_grid=n_grid, n_both_sides_uncrossed=n_ok, share=share)


def _improvements(rows: Sequence[V0MinPrediction]) -> list[Decimal]:
    return [row.rps_null - row.rps_model for row in rows]


def _verdict(mean: float, ci: tuple[float, float] | None) -> V0MinDecision:
    if ci is None:
        return V0MinDecision(
            rule="sign_test_oos_rps_improvement_vs_null",
            mean_improvement=mean,
            ci_low=None,
            ci_high=None,
            verdict="ci_undefined",
        )
    lo, hi = ci
    if lo > 0:
        verdict: Literal[
            "flow_carries_information", "flow_adds_nothing", "harmful_check_sign"
        ] = "flow_carries_information"
    elif hi < 0:
        verdict = "harmful_check_sign"
    else:
        verdict = "flow_adds_nothing"
    return V0MinDecision(
        rule="sign_test_oos_rps_improvement_vs_null",
        mean_improvement=mean,
        ci_low=lo,
        ci_high=hi,
        verdict=verdict,
    )


def _slice_scores(
    rows: Sequence[V0MinPrediction],
    key_of: Callable[[V0MinPrediction], str],
    *,
    seed: int,
    n_resample: int,
) -> tuple[SliceScore, ...]:
    grouped: dict[str, list[V0MinPrediction]] = {}
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


def _fit_on_days(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    train_days: Sequence[date],
    *,
    hours_to_close: Sequence[int],
    mapping: ObservedMapping | None,
    lambda_grid: Sequence[float],
) -> OffsetLogitFit | None:
    names, raw_obs = _collect_observations(
        trades,
        labels,
        train_days,
        hours_to_close=hours_to_close,
        mapping=mapping,
    )
    if names is None or not raw_obs:
        return None
    # Flatten x for standardisation across brackets and days.
    flat: list[list[float]] = []
    for _q, x_rows, _y in raw_obs:
        flat.extend(x_rows)
    _, mean, std = _standardize(flat)
    scaled_obs: list[tuple[list[float], list[list[float]], int]] = []
    for q, x_rows, y_idx in raw_obs:
        scaled, _, _ = _standardize(x_rows, mean=mean, std=std)
        scaled_obs.append((q, scaled, y_idx))
    n_days = len(train_days)
    sel_n = max(1, n_days // 5) if n_days >= 4 else 0
    if sel_n:
        fit_days = list(train_days)[:-sel_n]
        sel_days = list(train_days)[-sel_n:]
        _n_fit, fit_obs_raw = _collect_observations(
            trades,
            labels,
            fit_days,
            hours_to_close=hours_to_close,
            mapping=mapping,
        )
        _n_sel, sel_obs_raw = _collect_observations(
            trades,
            labels,
            sel_days,
            hours_to_close=hours_to_close,
            mapping=mapping,
        )
        fit_scaled: list[tuple[list[float], list[list[float]], int]] = []
        for q, x_rows, y_idx in fit_obs_raw:
            scaled, _, _ = _standardize(x_rows, mean=mean, std=std)
            fit_scaled.append((q, scaled, y_idx))
        sel_scaled: list[tuple[list[float], list[list[float]], int]] = []
        for q, x_rows, y_idx in sel_obs_raw:
            scaled, _, _ = _standardize(x_rows, mean=mean, std=std)
            sel_scaled.append((q, scaled, y_idx))
        lam = select_ridge_lambda(
            fit_scaled, sel_scaled, lambda_grid, feat_mean=mean, feat_std=std
        )
    else:
        lam = 1.0 if 1.0 in lambda_grid else float(lambda_grid[0])
    beta = fit_offset_logit_mle(scaled_obs, ridge_lambda=lam)
    return OffsetLogitFit(
        feature_names=tuple(names),
        beta=tuple(beta),
        ridge_lambda=lam,
        feat_mean=mean,
        feat_std=std,
        n_days=n_days,
    )


def _beta_intervals(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    train_days: Sequence[date],
    *,
    hours_to_close: Sequence[int],
    mapping: ObservedMapping | None,
    ridge_lambda: float,
    seed: int,
    n_resample: int,
) -> tuple[BetaInterval, ...]:
    fitted = _fit_on_days(
        trades,
        labels,
        train_days,
        hours_to_close=hours_to_close,
        mapping=mapping,
        lambda_grid=(ridge_lambda,),
    )
    if fitted is None:
        return ()
    days = list(train_days)
    if len(days) < 2:
        return tuple(
            BetaInterval(name=name, point=fitted.beta[i], ci_low=None, ci_high=None)
            for i, name in enumerate(fitted.feature_names)
        )
    import random

    rng = random.Random(seed)
    draws: list[list[float]] = [[] for _ in fitted.feature_names]
    n_boot = min(n_resample, 40)
    for _ in range(n_boot):
        sample_days = [days[rng.randrange(len(days))] for _ in days]
        refit = _fit_on_days(
            trades,
            labels,
            sample_days,
            hours_to_close=hours_to_close,
            mapping=mapping,
            lambda_grid=(ridge_lambda,),
        )
        if refit is None or refit.feature_names != fitted.feature_names:
            continue
        for i, value in enumerate(refit.beta):
            draws[i].append(value)
    out: list[BetaInterval] = []
    for i, name in enumerate(fitted.feature_names):
        series = [Decimal(str(v)) for v in draws[i]]
        ci = bootstrap_mean_ci(series, seed=seed + i, n_resample=min(n_resample, 200))
        out.append(
            BetaInterval(
                name=name,
                point=fitted.beta[i],
                ci_low=float(ci[0]) if ci else None,
                ci_high=float(ci[1]) if ci else None,
            )
        )
    return tuple(out)


def run_c1_m1_v0_min(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    prereg: Mapping[str, Any],
    prereg_dir: Path,
    ledger: Ledger,
    n_resample: int | None = None,
) -> V0MinReport:
    """Cross-tab → crossed rate → coverage → walk-forward scores. Never P&L."""
    registered = refuse_unless_preregistered(dict(prereg), prereg_dir)
    mapping = assert_outcome_bookside_mapping(
        [trade for trade in trades if not trade.is_block_trade]
    )
    # The cross-tab's off-diagonal is exactly zero, so it verified nothing about
    # direction. The crossed-state rate is the independent check, and it runs
    # before the first fit.
    crossed = crossed_diagnostic(trades)
    if crossed.verdict == "sign_inverted":
        raise InconsistentTakerMapping(
            f"crossed-state rate says the direction sign is backwards: {crossed.note}"
        )
    hours = tuple(
        int(h) for h in registered.get("hours_to_close") or DEFAULT_HOURS_TO_CLOSE
    )
    min_train = int(registered.get("walk_forward", {}).get("min_train_days") or 2)
    seed = int(registered.get("seed") or 0)
    n_boot = int(
        n_resample
        if n_resample is not None
        else registered.get("bootstrap", {}).get("n_resample") or 1000
    )
    lambda_grid = tuple(
        float(x) for x in (registered.get("ridge_lambda_grid") or RIDGE_LAMBDA_GRID)
    )
    clim_window = int(registered.get("climatology_doy_window") or 15)
    days = sorted({label.climate_day for label in labels.values()})
    coverage = trade_anchor_coverage(
        trades, labels, hours_to_close=hours, mapping=mapping
    )
    origins = list(
        expanding_origins(days, min_train_days=min_train, hours_to_close=hours)
    )
    if origins:
        assert_schedule_integrity(origins)

    predictions: list[V0MinPrediction] = []
    last_fit: OffsetLogitFit | None = None
    cache_key: tuple[date, ...] | None = None
    for origin in origins:
        if cache_key != origin.train_days:
            last_fit = _fit_on_days(
                trades,
                labels,
                origin.train_days,
                hours_to_close=hours,
                mapping=mapping,
                lambda_grid=lambda_grid,
            )
            cache_key = origin.train_days
        packed = _day_observation(
            trades, labels, origin.predict_day, origin.as_of, mapping=mapping
        )
        if packed is None:
            continue
        tickers, _q, x_rows, _y_idx, q_map = packed
        realised = _realised(labels, origin.predict_day)
        if realised is None:
            continue
        if last_fit is None:
            p_hat = dict(q_map)
        else:
            scaled, _, _ = _standardize(
                x_rows, mean=last_fit.feat_mean, std=last_fit.feat_std
            )
            p_hat = predict_adjusted(q_map, tickers, scaled, last_fit.beta)
        clim = climatology_forecast(
            origin.train_days,
            labels,
            origin.predict_day,
            tickers,
            window=clim_window,
        )
        rps_model = ranked_probability_score(p_hat, realised)
        rps_null = ranked_probability_score(q_map, realised)
        rps_clim = (
            ranked_probability_score(clim, realised) if clim is not None else None
        )
        predictions.append(
            V0MinPrediction(
                climate_day=origin.predict_day,
                as_of=origin.as_of,
                hours_to_close=origin.hours_to_close,
                season=season_of(origin.predict_day),
                realised=realised,
                p_hat=p_hat,
                q_null=q_map,
                climatology=clim,
                rps_model=rps_model,
                rps_null=rps_null,
                rps_climatology=rps_clim,
            )
        )

    improvements = _improvements(predictions)
    mean_imp = float(sum(improvements) / len(improvements)) if improvements else 0.0
    by_day: dict[date, list[Decimal]] = {}
    for row in predictions:
        by_day.setdefault(row.climate_day, []).append(row.rps_null - row.rps_model)
    clustered = clustered_bootstrap_mean_ci(by_day, seed=seed, n_resample=n_boot)
    contract = bootstrap_mean_ci(improvements, seed=seed, n_resample=n_boot)
    clustered_t = (
        (float(clustered[0]), float(clustered[1])) if clustered else None
    )
    contract_t = (float(contract[0]), float(contract[1])) if contract else None
    beta_iv: tuple[BetaInterval, ...] = ()
    if last_fit is not None and origins:
        beta_iv = _beta_intervals(
            trades,
            labels,
            origins[-1].train_days,
            hours_to_close=hours,
            mapping=mapping,
            ridge_lambda=last_fit.ridge_lambda,
            seed=seed,
            n_resample=n_boot,
        )
    report = V0MinReport(
        prereg_id=str(registered["prereg_id"]),
        crossed=crossed,
        coverage=coverage,
        n_predictions=len(predictions),
        mean_rps_improvement=mean_imp,
        clustered_ci=clustered_t,
        contract_ci=contract_t,
        decision=_verdict(mean_imp, clustered_t),
        by_season=_slice_scores(
            predictions, lambda row: row.season, seed=seed, n_resample=n_boot
        ),
        beta=beta_iv,
    )
    ledger.record(
        "C1_M1_V0_MIN",
        {
            "prereg_id": report.prereg_id,
            "crossed_rate": report.crossed.as_specified.rate,
            "crossed_rate_inverted": report.crossed.inverted.rate,
            "crossed_verdict": report.crossed.verdict,
            "n_predictions": report.n_predictions,
            "coverage_share": report.coverage.share,
            "mean_rps_improvement": report.mean_rps_improvement,
            "verdict": report.decision.verdict,
            "is_strategy_pnl": False,
        },
    )
    return report


def residual_gbm_experiment(report: V0MinReport) -> dict[str, object]:
    """Second experiment. Same walk-forward/clustered eval, on GLM residuals."""
    if report.decision.verdict != "flow_carries_information":
        raise ValueError(
            "residual GBM is the second experiment; "
            f"sign test verdict is {report.decision.verdict}"
        )
    return {
        "status": "NOT_RUN",
        "reason": "sign test passed in-process; live corpus GBM is pass 2",
        "is_strategy_pnl": False,
    }


def public_callables() -> tuple[str, ...]:
    """Names of the v0-MINIMAL public surface. Used by the no-currency canary."""
    return (
        "run_c1_m1_v0_min",
        "fit_offset_logit_mle",
        "predict_adjusted",
        "climatology_forecast",
        "trade_anchor_coverage",
        "residual_gbm_experiment",
        "null_trade_recovery",
    )
