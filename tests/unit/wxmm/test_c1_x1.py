"""C1-X1 goldens, ingest merge, X1a/X1b/X1c, gates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    assert_go_no_go_filled,
    attribute_trade,
    bootstrap_mean_ci,
    clustered_bootstrap_mean_ci,
    compact_from_attributed,
    select_primary,
    select_primary_compact,
    x1a_report,
    x1a_report_compact,
)
from wxmm.analysis.population import x1b_report
from wxmm.analysis.study import run_c1_x1
from wxmm.analysis.trades_ingest import (
    boundary_audit_duckdb,
    fetch_cutoff,
    ladder_regime_for,
    merge_trades,
    parse_climate_day,
    parse_trade,
    pull_ticker,
)
from wxmm.backtest.ledger import Ledger
from wxmm.core.errors import GoNoGoNotFilled, PriceComplementError
from wxmm.core.money import Money
from wxmm.eval.flb import _ols_clustered, resting_offers_below_10c, x1c_report
from wxmm.venues.kalshi.fees import FeeRounding, maker_fee, taker_fee

PREREG = Path(__file__).resolve().parents[3] / "prereg"


def _payload(
    *,
    trade_id: str = "t1",
    ticker: str = "KXHIGHNY-26AUG12-T90",
    count: str = "100.00",
    yes: str = "0.4000",
    no: str = "0.6000",
    outcome: str = "yes",
    book: str = "ask",
    created: str = "2026-08-12T14:00:00Z",
    block: bool = False,
) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "count_fp": count,
        "yes_price_dollars": yes,
        "no_price_dollars": no,
        "taker_outcome_side": outcome,
        "taker_book_side": book,
        "created_time": created,
        "is_block_trade": block,
    }


def _trade(**kwargs: object):
    source = kwargs.pop("source_endpoint", "historical")
    return parse_trade(_payload(**kwargs), source_endpoint=source)  # type: ignore[arg-type]


def _label(ticker: str = "KXHIGHNY-26AUG12-T90", *, yes_won: bool = True) -> SettlementLabel:
    return SettlementLabel(
        ticker=ticker,
        climate_day=parse_climate_day(ticker),
        yes_won=yes_won,
        source="clinyc_as_issued",
    )


def test_hand_computed_golden_gross_and_net() -> None:
    """Taker lifts YES ask at 40¢, 100 contracts, YES settles. Hand numbers."""
    trade = _trade()
    row = attribute_trade(trade, _label())
    # Taker long YES at 0.40, y=1.
    assert row.taker.gross_return == Decimal("1.5")
    assert row.taker.gross_pnl == Decimal("60")
    # 0.07 * 100 * 0.40 * 0.60 = 1.68 → already on the cent.
    assert row.taker.fee == Decimal("1.68")
    assert row.taker.net_pnl == Decimal("58.32")
    assert row.taker.net_return == (Decimal("1") - Decimal("0.40") - Decimal("0.0168")) / (
        Decimal("0.40") + Decimal("0.0168")
    )
    # Maker long NO at 0.60, y=0.
    assert row.maker.gross_return == Decimal("-1")
    assert row.maker.gross_pnl == Decimal("-60")
    assert row.maker.fee == Decimal("0")
    assert row.maker.net_return == Decimal("-1")
    assert row.maker.is_strategy_pnl is False
    assert row.is_strategy_pnl is False


def test_complement_assertion_raises_on_docs_placeholder() -> None:
    with pytest.raises(PriceComplementError):
        parse_trade(
            _payload(yes="0.5600", no="0.5600"),
            source_endpoint="live",
        )


def test_sign_settled_yes_taker_lifts_ask() -> None:
    row = attribute_trade(_trade(), _label(yes_won=True))
    assert row.taker.gross_pnl > 0
    assert row.maker.gross_pnl < 0
    assert row.taker.gross_pnl + row.maker.gross_pnl == Decimal("0")
    assert row.taker.net_pnl + row.maker.net_pnl == -(row.taker.fee + row.maker.fee)


def test_maker_fee_zero_taker_fee_both_roundings() -> None:
    assert maker_fee(1, Decimal("0.50")) == Money.zero()
    assert maker_fee(500, Decimal("0.10")) == Money.zero()
    for contracts in (1, 100, 500):
        for price in (Decimal("0.10"), Decimal("0.50")):
            raw = Decimal("0.07") * Decimal(contracts) * price * (Decimal("1") - price)
            cont = taker_fee(contracts, price, FeeRounding.CONTINUOUS)
            cent = taker_fee(contracts, price, FeeRounding.PER_ORDER_CENT)
            assert cont == Money.dollars(raw)
            assert cent == Money.dollars(raw).round_up_to_cent()


def test_era_boundary_2022_12_10_vs_11() -> None:
    early = _trade(ticker="HIGHNY-22DEC10-T80", created="2022-12-10T15:00:00Z")
    late = _trade(ticker="HIGHNY-22DEC11-T80", created="2022-12-11T15:00:00Z")
    assert parse_climate_day(early.ticker) == date(2022, 12, 10)
    assert parse_climate_day(late.ticker) == date(2022, 12, 11)
    assert ladder_regime_for(early.climate_day) == "pre_six_bracket"
    assert ladder_regime_for(late.climate_day) == "six_bracket"
    assert early.ladder_regime != late.ladder_regime


def test_missing_canonical_fields_raises() -> None:
    raw = _payload()
    del raw["taker_outcome_side"]
    with pytest.raises(ValueError, match="canonical"):
        parse_trade(raw, source_endpoint="historical")


def test_cutoff_then_merge_detects_duplicate_and_gap() -> None:
    class Fake:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            if path == "/historical/cutoff":
                return {"market_settled_ts": 1_700_000_000}
            if path == "/historical/trades":
                return {
                    "trades": [
                        _payload(
                            trade_id="h1",
                            created="2023-11-14T00:00:00Z",
                        )
                    ],
                    "cursor": "",
                }
            return {
                "trades": [
                    _payload(
                        trade_id="h1",
                        created="2023-11-15T12:00:00Z",
                    ),
                    _payload(
                        trade_id="l1",
                        created="2023-11-15T12:00:00Z",
                    ),
                ],
                "cursor": "",
            }

    cutoff = fetch_cutoff(Fake())
    assert cutoff.market_settled_ts == 1_700_000_000
    hist, live = pull_ticker(Fake(), "KXHIGHNY-26AUG12-T90", cutoff)
    merged, report = merge_trades(hist, live, cutoff)
    assert report.n_duplicate_ids == 1
    assert "h1" in report.duplicate_ids
    assert len(merged) == 2
    assert boundary_audit_duckdb(hist, live) == 1


def test_day_clustered_bootstrap_wider_than_trade_level() -> None:
    day_a = [Decimal("0")] * 40
    day_b = [Decimal("1")] * 40
    trade_ci = bootstrap_mean_ci(day_a + day_b, seed=0, n_resample=400)
    cluster_ci = clustered_bootstrap_mean_ci(
        {date(2026, 8, 1): day_a, date(2026, 8, 2): day_b},
        seed=0,
        n_resample=400,
    )
    assert trade_ci is not None and cluster_ci is not None
    trade_width = trade_ci[1] - trade_ci[0]
    cluster_width = cluster_ci[1] - cluster_ci[0]
    assert cluster_width > trade_width


def test_x1b_flat_vs_directional_distribution() -> None:
    labels = {
        "KXHIGHNY-26AUG12-T90": _label("KXHIGHNY-26AUG12-T90"),
        "KXHIGHNY-26AUG13-T90": _label("KXHIGHNY-26AUG13-T90"),
    }
    flat_buy = _trade(trade_id="b", ticker="KXHIGHNY-26AUG12-T90", book="bid")
    flat_sell = _trade(trade_id="s", ticker="KXHIGHNY-26AUG12-T90", book="ask")
    directional = _trade(
        trade_id="d",
        ticker="KXHIGHNY-26AUG13-T90",
        created="2026-08-13T14:00:00Z",
        book="ask",
    )
    primary, blocks, _ = select_primary(
        [flat_buy, flat_sell, directional],
        labels,
    )
    assert blocks == []
    report = x1b_report(primary)
    assert report.n_bracket_days == 2
    assert report.is_strategy_pnl is False
    # One day |net|/gross = 0, one day = 1.
    ten = next(row for row in report.thresholds if row.threshold == Decimal("0.10"))
    assert ten.n_below == 1
    assert ten.share_below == Decimal("0.5")


def test_x1c_resting_offers_below_10c_and_ols() -> None:
    est = _ols_clustered(
        y=[-1.0, 0.0, 2.0, 3.0],
        x=[0.0, 1.0, 2.0, 3.0],
        clusters=["a", "a", "b", "b"],
    )
    assert est.n == 4
    assert abs(est.psi - 1.4) < 1e-9
    cheap = _trade(trade_id="w", yes="0.0500", no="0.9500", book="ask")
    mid = _trade(trade_id="m", yes="0.4000", no="0.6000", book="ask")
    labels = {"KXHIGHNY-26AUG12-T90": _label()}
    primary, _, _ = select_primary([cheap, mid], labels)
    wings = resting_offers_below_10c(primary)
    assert len(wings) == 1
    report = x1c_report(primary)
    assert report.resting_offer_below_10c_n == 1
    assert report.published_n == 29924
    assert report.published_psi == Decimal("0.031")


def test_x1a_excludes_blocks_and_pre_era() -> None:
    labels = {
        "KXHIGHNY-26AUG12-T90": _label(),
        "HIGHNY-22DEC10-T80": SettlementLabel(
            ticker="HIGHNY-22DEC10-T80",
            climate_day=date(2022, 12, 10),
            yes_won=True,
            source="clinyc_as_issued",
        ),
    }
    primary, blocks, unlabelled = select_primary(
        [
            _trade(block=True, trade_id="blk"),
            _trade(ticker="HIGHNY-22DEC10-T80", created="2022-12-10T15:00:00Z", trade_id="old"),
            _trade(trade_id="keep"),
        ],
        labels,
    )
    assert len(primary) == 1
    assert primary[0].trade_id == "keep"
    assert len(blocks) == 1
    assert unlabelled == 0
    report = x1a_report(primary, blocks, n_unlabelled=0, seed=0, n_resample=50)
    assert report.n_primary_trades == 1
    assert report.n_block_trades == 1
    assert report.is_strategy_pnl is False


def test_go_no_go_fill_in_refuses_run() -> None:
    payload = yaml.safe_load((PREREG / "c1-x1-v1.yaml").read_text())
    with pytest.raises(GoNoGoNotFilled):
        assert_go_no_go_filled(payload)
    ledger = Ledger()
    with pytest.raises(GoNoGoNotFilled):
        run_c1_x1([], {}, prereg=payload, prereg_dir=PREREG, ledger=ledger)


def test_determinism_same_seed_same_bytes() -> None:
    labels = {"KXHIGHNY-26AUG12-T90": _label()}
    trades = [
        _trade(trade_id="a"),
        _trade(trade_id="b", created="2026-08-12T15:00:00Z"),
        _trade(
            trade_id="c",
            ticker="KXHIGHNY-26AUG13-T90",
            created="2026-08-13T14:00:00Z",
        ),
    ]
    labels["KXHIGHNY-26AUG13-T90"] = _label("KXHIGHNY-26AUG13-T90")
    primary, blocks, n_unlabelled = select_primary(trades, labels)
    a = x1a_report(primary, blocks, n_unlabelled=n_unlabelled, seed=7, n_resample=80)
    b = x1a_report(primary, blocks, n_unlabelled=n_unlabelled, seed=7, n_resample=80)
    assert a.model_dump() == b.model_dump()
    xb1 = x1b_report(primary)
    xb2 = x1b_report(primary)
    assert xb1.model_dump() == xb2.model_dump()


def test_prereg_id_is_registered() -> None:
    payload = yaml.safe_load((PREREG / "c1-x1-v1.yaml").read_text())
    assert payload["prereg_id"] == "c1-x1-v1"
    from wxmm.backtest.ledger import refuse_unless_preregistered

    registered = refuse_unless_preregistered(payload, PREREG)
    assert registered["prereg_id"] == "c1-x1-v1"


def test_compact_path_matches_attributed_on_golden() -> None:
    labels = {
        "KXHIGHNY-26AUG12-T90": _label("KXHIGHNY-26AUG12-T90"),
        "KXHIGHNY-26AUG13-T90": _label("KXHIGHNY-26AUG13-T90", yes_won=False),
    }
    trades = [
        _trade(trade_id="a"),
        _trade(trade_id="b", created="2026-08-12T15:00:00Z", book="bid"),
        _trade(
            trade_id="c",
            ticker="KXHIGHNY-26AUG13-T90",
            created="2026-08-13T14:00:00Z",
        ),
    ]
    primary, blocks, n_unlabelled = select_primary(trades, labels)
    compact_p, compact_b, compact_u, skipped = select_primary_compact(trades, labels)
    assert skipped == 0
    assert compact_u == n_unlabelled
    assert len(compact_p) == len(primary)
    assert len(compact_b) == len(blocks)
    a = x1a_report(primary, blocks, n_unlabelled=n_unlabelled, seed=7, n_resample=80)
    b = x1a_report_compact(
        compact_p, compact_b, n_unlabelled=compact_u, seed=7, n_resample=80
    )
    assert a.n_primary_trades == b.n_primary_trades
    assert float(a.maker_mean_net) == pytest.approx(float(b.maker_mean_net), rel=1e-9)
    assert a.maker_ci_net is not None and b.maker_ci_net is not None
    assert float(a.maker_ci_net[0]) == pytest.approx(float(b.maker_ci_net[0]), rel=1e-6)
    from wxmm.analysis.population import bracket_day_nets, bracket_day_nets_compact
    from wxmm.eval.flb import x1c_report, x1c_report_compact

    nets_a = bracket_day_nets(primary)
    nets_b = bracket_day_nets_compact(compact_p)
    assert len(nets_a) == len(nets_b)
    assert x1b_report(primary).n_bracket_days == len(nets_b)
    xc_a = x1c_report(primary)
    xc_b = x1c_report_compact(compact_p)
    assert xc_a.estimate.n == xc_b.estimate.n
    assert compact_from_attributed(primary[0]).ticker == primary[0].ticker
