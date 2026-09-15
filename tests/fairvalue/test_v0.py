"""C1-M1 v0: coverage floor, null recovery, walk-forward, scores, no P&L."""

from __future__ import annotations

import inspect
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
from wxmm.analysis.trades_ingest import parse_climate_day, parse_trade
from wxmm.backtest.ledger import Ledger
from wxmm.core.errors import (
    CoverageFloorRefused,
    LeakageError,
    NwpInterpolationUnspecified,
)
from wxmm.eval.scores import brier_decomposition
from wxmm.fairvalue.anchor_trades import implied_book_from_trades
from wxmm.fairvalue.coverage import refuse_unless_coverage, trade_anchor_coverage
from wxmm.fairvalue.features import nwp_features
from wxmm.fairvalue.ladder import assert_normalised
from wxmm.fairvalue.model_trades import (
    null_trade_recovery,
    trade_ladder_or_none,
)
from wxmm.fairvalue.v0 import run_c1_m1_v0
from wxmm.fairvalue.v0_baselines import nbm_forecast
from wxmm.fairvalue.walkforward import assert_schedule_integrity, expanding_origins
from wxmm.settlement.eras import kalshi_last_trading_close_utc

UTC = timezone.utc
ROOT_PREREG = Path(__file__).resolve().parents[2] / "prereg"


def _raw(
    *,
    trade_id: str,
    ticker: str,
    outcome: str,
    book: str,
    yes: str,
    no: str,
    created: datetime,
    block: bool = False,
):
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": "10.00",
            "yes_price_dollars": yes,
            "no_price_dollars": no,
            "taker_outcome_side": outcome,
            "taker_book_side": book,
            "created_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_block_trade": block,
        },
        source_endpoint="historical",
    )


def _pair(ticker: str, stamp: datetime, yes: str = "0.4000", no_yes: str = "0.3500"):
    no_comp = f"{(Decimal('1') - Decimal(yes)):.4f}"
    no_no = f"{(Decimal('1') - Decimal(no_yes)):.4f}"
    return [
        _raw(
            trade_id=f"{ticker}-y-{stamp.isoformat()}",
            ticker=ticker,
            outcome="yes",
            book="ask",
            yes=yes,
            no=no_comp,
            created=stamp,
        ),
        _raw(
            trade_id=f"{ticker}-n-{stamp.isoformat()}",
            ticker=ticker,
            outcome="no",
            book="bid",
            yes=no_yes,
            no=no_no,
            created=stamp + timedelta(minutes=5),
        ),
    ]


def _label(ticker: str, *, yes_won: bool) -> SettlementLabel:
    return SettlementLabel(
        ticker=ticker,
        climate_day=parse_climate_day(ticker),
        yes_won=yes_won,
        source="clinyc_as_issued",
    )


def _days() -> list[date]:
    return [date(2026, 8, d) for d in range(10, 16)]


def _tickers(day: date) -> tuple[str, str]:
    tag = day.strftime("%y%b%d").upper()
    return f"KXHIGHNY-{tag}-T80", f"KXHIGHNY-{tag}-T90"


def _corpus() -> tuple[list, dict[str, SettlementLabel]]:
    trades = []
    labels: dict[str, SettlementLabel] = {}
    for i, day in enumerate(_days()):
        t80, t90 = _tickers(day)
        stamp = kalshi_last_trading_close_utc(day) - timedelta(hours=30)
        trades.extend(_pair(t80, stamp, yes="0.3000", no_yes="0.2500"))
        trades.extend(_pair(t90, stamp + timedelta(minutes=20), yes="0.4500", no_yes="0.4000"))
        labels[t80] = _label(t80, yes_won=(i % 2 == 0))
        labels[t90] = _label(t90, yes_won=(i % 2 == 1))
    return trades, labels


def _prereg(tmp_path: Path, **overrides: object) -> tuple[dict, Path]:
    payload = yaml.safe_load((ROOT_PREREG / "c1-m1-v0.yaml").read_text())
    payload["prereg_id"] = "c1-m1-v0-test"
    payload["walk_forward"]["min_train_days"] = 2
    payload["hours_to_close"] = [24, 12]
    payload["bootstrap"]["n_resample"] = 40
    payload.update(overrides)
    path = tmp_path / "c1-m1-v0-test.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return payload, tmp_path


def test_prereg_is_registered() -> None:
    payload = yaml.safe_load((ROOT_PREREG / "c1-m1-v0.yaml").read_text())
    from wxmm.backtest.ledger import refuse_unless_preregistered

    registered = refuse_unless_preregistered(payload, ROOT_PREREG)
    assert registered["prereg_id"] == "c1-m1-v0"
    assert registered["coverage_floor"] == 0.15
    assert 24 in registered["hours_to_close"] and 12 in registered["hours_to_close"]
    assert registered["decision"]["type"] == "sign_test"


def test_null_recovery_matches_normalised_ladder() -> None:
    trades, _labels = _corpus()
    day = _days()[0]
    tickers = list(_tickers(day))
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=24)
    ladder = trade_ladder_or_none(trades, tickers, as_of=as_of)
    assert ladder is not None
    recovered = null_trade_recovery(ladder)
    raw = {ticker: ladder.books[ticker].mid for ticker in tickers}
    assert all(v is not None for v in raw.values())
    total = sum(raw.values(), Decimal("0"))
    normalised = {k: v / total for k, v in raw.items()}  # type: ignore[operator]
    for key in tickers:
        assert abs(recovered[key] - normalised[key]) < Decimal("1e-12")
    assert_normalised(recovered)


def test_null_recovery_mutant_raw_mid_fails() -> None:
    trades, _labels = _corpus()
    day = _days()[0]
    tickers = list(_tickers(day))
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=24)
    ladder = trade_ladder_or_none(trades, tickers, as_of=as_of)
    assert ladder is not None
    recovered = null_trade_recovery(ladder)
    mutant = {ticker: ladder.books[ticker].mid for ticker in tickers}
    with pytest.raises(AssertionError):
        assert_normalised({k: v for k, v in mutant.items() if v is not None})
    assert sum(recovered.values(), Decimal("0")) == Decimal("1")
    assert abs(sum(v for v in mutant.values() if v is not None) - Decimal("1")) > Decimal(
        "1e-6"
    )


def test_offset_as_of_raises_on_future_print() -> None:
    trades, _labels = _corpus()
    day = _days()[0]
    as_of = kalshi_last_trading_close_utc(day) - timedelta(hours=24)
    future = _raw(
        trade_id="future",
        ticker=_tickers(day)[0],
        outcome="yes",
        book="ask",
        yes="0.9000",
        no="0.1000",
        created=as_of + timedelta(minutes=1),
    )
    with pytest.raises(LeakageError, match="created_time after as_of"):
        implied_book_from_trades(trades + [future], as_of=as_of, ticker=_tickers(day)[0])


def test_walkforward_integrity_property() -> None:
    days = _days()
    origins = list(expanding_origins(days, min_train_days=2, hours_to_close=(24, 12)))
    assert_schedule_integrity(origins)
    for origin in origins:
        assert max(origin.train_days) < origin.predict_day
        assert origin.as_of == kalshi_last_trading_close_utc(origin.predict_day) - timedelta(
            hours=origin.hours_to_close
        )


def test_day_clustered_bootstrap_is_wider() -> None:
    groups = {
        date(2026, 8, 10): [Decimal("1"), Decimal("1")],
        date(2026, 8, 11): [Decimal("-1"), Decimal("-1")],
        date(2026, 8, 12): [Decimal("0.5"), Decimal("0.5")],
    }
    flat = [item for row in groups.values() for item in row]
    clustered = clustered_bootstrap_mean_ci(groups, seed=0, n_resample=400)
    contract = bootstrap_mean_ci(flat, seed=0, n_resample=400)
    assert clustered is not None and contract is not None
    clustered_width = clustered[1] - clustered[0]
    contract_width = contract[1] - contract[0]
    assert clustered_width > contract_width


def test_brier_decomposition_reconciles() -> None:
    forecasts = [
        {"A": Decimal("0.7"), "B": Decimal("0.3")},
        {"A": Decimal("0.2"), "B": Decimal("0.8")},
        {"A": Decimal("0.5"), "B": Decimal("0.5")},
    ]
    a = brier_decomposition(forecasts, ["A", "B", "A"])
    b = brier_decomposition(forecasts, ["A", "B", "A"])
    assert a.reconcile()
    assert abs(float(a.uncertainty) - float(b.uncertainty)) <= 0.01 * float(a.uncertainty)


def test_coverage_floor_refuses_fit(tmp_path: Path) -> None:
    day = date(2026, 8, 12)
    ticker = _tickers(day)[0]
    one_sided = [
        _raw(
            trade_id="only-yes",
            ticker=ticker,
            outcome="yes",
            book="ask",
            yes="0.4000",
            no="0.6000",
            created=kalshi_last_trading_close_utc(day) - timedelta(hours=2),
        )
    ]
    other = [
        _raw(
            trade_id="only-no",
            ticker=_tickers(day)[1],
            outcome="no",
            book="bid",
            yes="0.3500",
            no="0.6500",
            created=kalshi_last_trading_close_utc(day) - timedelta(hours=2),
        )
    ]
    from wxmm.fairvalue.anchor_trades import assert_outcome_bookside_mapping

    mapping = assert_outcome_bookside_mapping(one_sided + other)
    coverage = trade_anchor_coverage(one_sided + other, mapping=mapping, floor=0.15)
    assert coverage.two_sided_share < 0.15
    with pytest.raises(CoverageFloorRefused):
        refuse_unless_coverage(coverage)
    payload, prereg_dir = _prereg(tmp_path)
    labels = {
        _tickers(day)[0]: _label(_tickers(day)[0], yes_won=True),
        _tickers(day)[1]: _label(_tickers(day)[1], yes_won=False),
    }
    with pytest.raises(CoverageFloorRefused):
        run_c1_m1_v0(
            one_sided + other,
            labels,
            prereg=payload,
            prereg_dir=prereg_dir,
            ledger=Ledger(),
            n_resample=20,
        )


def test_nwp_and_nbm_are_unavailable() -> None:
    with pytest.raises(NwpInterpolationUnspecified, match="UNAVAILABLE_INTERPOLATION"):
        nwp_features()
    with pytest.raises(NwpInterpolationUnspecified, match="UNAVAILABLE_INTERPOLATION"):
        nbm_forecast()


def test_run_v0_scores_not_pnl_and_deterministic(tmp_path: Path) -> None:
    trades, labels = _corpus()
    payload, prereg_dir = _prereg(tmp_path)
    a = run_c1_m1_v0(
        trades, labels, prereg=payload, prereg_dir=prereg_dir, ledger=Ledger(), n_resample=30
    )
    b = run_c1_m1_v0(
        trades, labels, prereg=payload, prereg_dir=prereg_dir, ledger=Ledger(), n_resample=30
    )
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.is_strategy_pnl is False
    assert a.nbm_status == "UNAVAILABLE_INTERPOLATION_UNSPECIFIED"
    assert a.n_predictions > 0
    assert a.coverage.two_sided_share >= 0.15
    assert a.brier_null_reconciles
    assert a.unc_common_within_1pct
    if a.clustered_ci is not None and a.contract_ci is not None:
        clustered_width = a.clustered_ci[1] - a.clustered_ci[0]
        contract_width = a.contract_ci[1] - a.contract_ci[0]
        assert clustered_width + 1e-12 >= contract_width
    assert "JJA" in {row.key for row in a.by_season}


def test_public_surface_has_no_currency_return() -> None:
    from wxmm.core.money import Money
    from wxmm.fairvalue import coverage, v0, v0_baselines, walkforward

    modules = (v0, coverage, v0_baselines, walkforward)
    for module in modules:
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_"):
                continue
            hints = inspect.get_annotations(fn)
            ret = hints.get("return")
            text = str(ret)
            assert Money not in (ret, getattr(ret, "__origin__", None))
            assert "Money" not in text
            assert "PnL" not in text and "pnl" not in name.lower()


def test_rps_orders_by_ladder_not_ticker_string() -> None:
    from wxmm.eval.scores import ranked_probability_score
    from wxmm.fairvalue.v0 import _rps_on_ladder

    less = "HIGHNY-26AUG12-T70"
    between = "HIGHNY-26AUG12-B80.5"
    greater = "HIGHNY-26AUG12-T83"
    forecast = {
        less: Decimal("0.80"),
        between: Decimal("0.10"),
        greater: Decimal("0.10"),
    }
    assert ranked_probability_score(forecast, less) != _rps_on_ladder(forecast, less)


def test_null_t_tail_is_less_not_greater() -> None:
    from analysis.v0_labels import labels_from_clinyc, resolve_strike

    day = [
        {"ticker": "HIGHNY-22DEC11-B38.5", "strike_type": None, "result": "no"},
        {"ticker": "HIGHNY-22DEC11-B40.5", "strike_type": None, "result": "yes"},
        {"ticker": "HIGHNY-22DEC11-B42.5", "strike_type": None, "result": "no"},
        {"ticker": "HIGHNY-22DEC11-B44.5", "strike_type": None, "result": "no"},
        {"ticker": "HIGHNY-22DEC11-T38", "strike_type": None, "result": "no"},
        {"ticker": "HIGHNY-22DEC11-T45", "strike_type": None, "result": "no"},
    ]
    assert resolve_strike(day[4], day) == ("less", None, 38)
    assert resolve_strike(day[5], day) == ("greater", 45, None)
    labels, diag = labels_from_clinyc(
        day,
        {"2022-12-11": {"high_F": 40, "climate_date": "2022-12-11"}},
    )
    assert diag["n_unique_winner_days"] == 1
    assert labels["HIGHNY-22DEC11-B40.5"].yes_won is True
    assert labels["HIGHNY-22DEC11-T38"].yes_won is False
    assert diag["n_venue_result_disagree"] == 0
