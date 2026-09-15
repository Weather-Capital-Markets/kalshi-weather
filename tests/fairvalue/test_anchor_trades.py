"""Trade-derived anchor: mapping gate, sign golden, crossed, provenance."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wxmm.analysis.trades_ingest import parse_trade
from wxmm.backtest.ledger import Ledger
from wxmm.core.errors import (
    GoNoGoNotFilled,
    InconsistentTakerMapping,
    LeakageError,
    PriceComplementError,
    ReconstructionBoundRequired,
)
from wxmm.eval.baselines import context_with_bound
from wxmm.fairvalue import model as fairvalue_model
from wxmm.fairvalue.anchor_trades import (
    ObservedMapping,
    assert_outcome_bookside_mapping,
    implied_book_from_trades,
    trade_derived_ladder,
    yes_space_print,
)
from wxmm.fairvalue.features import (
    assert_staleness_surfaced,
    book_features,
    calendar_features,
    flow_features,
    flow_features_multiwindow,
)
from wxmm.fairvalue.mid_compare import compare_reconstructed_vs_trade
from wxmm.fairvalue.model_trades import fit_trade_derived, null_trade_recovery

UTC = timezone.utc
PREREG = Path(__file__).resolve().parents[2] / "prereg"
AS_OF = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)


def _raw(
    *,
    trade_id: str,
    outcome: str,
    book: str,
    yes: str,
    no: str,
    created: str,
    block: bool = False,
    ticker: str = "KXHIGHNY-26AUG12-T90",
    count: str = "10.00",
):
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": count,
            "yes_price_dollars": yes,
            "no_price_dollars": no,
            "taker_outcome_side": outcome,
            "taker_book_side": book,
            "created_time": created,
            "is_block_trade": block,
        },
        source_endpoint="historical",
    )


def _clean_pair(
    *,
    yes_px: str = "0.4000",
    no_comp: str = "0.6000",
    no_yes_px: str = "0.3500",
    no_no_px: str = "0.6500",
    t_yes: str = "2026-08-12T14:00:00Z",
    t_no: str = "2026-08-12T15:00:00Z",
    ticker: str = "KXHIGHNY-26AUG12-T90",
):
    return [
        _raw(
            trade_id=f"{ticker}-y",
            outcome="yes",
            book="ask",
            yes=yes_px,
            no=no_comp,
            created=t_yes,
            ticker=ticker,
        ),
        _raw(
            trade_id=f"{ticker}-n",
            outcome="no",
            book="bid",
            yes=no_yes_px,
            no=no_no_px,
            created=t_no,
            ticker=ticker,
        ),
    ]


def test_mapping_clean_bijection() -> None:
    observed = assert_outcome_bookside_mapping(_clean_pair())
    assert observed.status == "clean"
    assert observed.outcome_to_book == {"yes": "ask", "no": "bid"}


def test_mapping_one_cell_is_not_a_bijection() -> None:
    only_yes = [
        _raw(
            trade_id="only-yes",
            outcome="yes",
            book="ask",
            yes="0.4000",
            no="0.6000",
            created="2026-08-12T14:00:00Z",
        )
    ]
    with pytest.raises(InconsistentTakerMapping, match="not a complete"):
        assert_outcome_bookside_mapping(only_yes)


def test_mapping_antidiagonal_is_clean() -> None:
    anti = [
        _raw(
            trade_id="yes-bid",
            outcome="yes",
            book="bid",
            yes="0.4000",
            no="0.6000",
            created="2026-08-12T14:00:00Z",
        ),
        _raw(
            trade_id="no-ask",
            outcome="no",
            book="ask",
            yes="0.3500",
            no="0.6500",
            created="2026-08-12T15:00:00Z",
        ),
    ]
    observed = assert_outcome_bookside_mapping(anti)
    assert observed.outcome_to_book == {"yes": "bid", "no": "ask"}


def test_mapping_inconsistent_raises() -> None:
    dirty = _clean_pair() + [
        _raw(
            trade_id="dirty",
            outcome="yes",
            book="bid",
            yes="0.4100",
            no="0.5900",
            created="2026-08-12T16:00:00Z",
        )
    ]
    with pytest.raises(InconsistentTakerMapping, match="yes maps to both"):
        assert_outcome_bookside_mapping(dirty)


def test_sign_golden_no_taker_sets_bid() -> None:
    no_trade = _raw(
        trade_id="no1",
        outcome="no",
        book="bid",
        yes="0.4200",
        no="0.5800",
        created="2026-08-12T15:00:00Z",
    )
    print_ = yes_space_print(no_trade)
    assert print_.direction == -1
    assert print_.yes_price == Decimal("1") - Decimal("0.5800")
    mapping = ObservedMapping(
        counts={("yes", "ask"): 1, ("yes", "bid"): 0, ("no", "ask"): 0, ("no", "bid"): 1},
        outcome_to_book={"yes": "ask", "no": "bid"},
        n_non_block=2,
        status="clean",
    )
    book = implied_book_from_trades([no_trade], as_of=AS_OF, mapping=mapping)
    assert book.bid == Decimal("0.4200")
    assert book.ask is None
    assert book.provenance == "TRADE_DERIVED"


def test_as_of_future_print_raises_leakage() -> None:
    future = _raw(
        trade_id="future",
        outcome="no",
        book="bid",
        yes="0.4200",
        no="0.5800",
        created="2026-08-12T19:00:00Z",
    )
    with pytest.raises(LeakageError, match="available_at"):
        implied_book_from_trades([future], as_of=AS_OF)


def test_complement_placeholder_raises() -> None:
    with pytest.raises(PriceComplementError):
        _raw(
            trade_id="bad",
            outcome="yes",
            book="ask",
            yes="0.5600",
            no="0.5600",
            created="2026-08-12T14:00:00Z",
        )


def test_crossed_invalidates_older_side() -> None:
    trades = [
        _raw(
            trade_id="ask",
            outcome="yes",
            book="ask",
            yes="0.4000",
            no="0.6000",
            created="2026-08-12T14:00:00Z",
        ),
        _raw(
            trade_id="bid",
            outcome="no",
            book="bid",
            yes="0.5000",
            no="0.5000",
            created="2026-08-12T15:00:00Z",
        ),
    ]
    book = implied_book_from_trades(trades, as_of=AS_OF)
    assert book.crossed_invalidations == 1
    assert book.ask is None
    assert book.bid == Decimal("0.5000")
    assert book.mid is None


def test_staleness_is_per_side_and_must_be_surfaced() -> None:
    trades = _clean_pair()
    book = implied_book_from_trades(trades, as_of=AS_OF)
    assert book.bid_staleness is not None
    assert book.ask_staleness is not None
    assert book.bid_staleness != book.ask_staleness
    assert book.mid is not None
    feats = flow_features(trades, as_of=AS_OF, window=timedelta(hours=6))
    payload = feats.as_dict()
    assert_staleness_surfaced(payload)
    dropped = {k: v for k, v in payload.items() if "stale" not in k}
    with pytest.raises(AssertionError, match="staleness dropped"):
        assert_staleness_surfaced(dropped)


def test_missing_is_not_zero() -> None:
    book = implied_book_from_trades([], as_of=AS_OF)
    assert book.mid is None
    assert book.mid != Decimal("0")
    assert trade_derived_ladder({"A": book}) is None


def test_block_trades_do_not_move_book() -> None:
    mapping = ObservedMapping(
        counts={("yes", "ask"): 1, ("yes", "bid"): 0, ("no", "ask"): 0, ("no", "bid"): 1},
        outcome_to_book={"yes": "ask", "no": "bid"},
        n_non_block=2,
        status="clean",
    )
    block = _raw(
        trade_id="blk",
        outcome="yes",
        book="ask",
        yes="0.4000",
        no="0.6000",
        created="2026-08-12T14:00:00Z",
        block=True,
    )
    book = implied_book_from_trades([block], as_of=AS_OF, mapping=mapping)
    assert book.ask is None
    assert book.bid is None
    assert book.n_block_excluded == 1
    after = implied_book_from_trades(
        _clean_pair()
        + [
            _raw(
                trade_id="blk-later",
                outcome="yes",
                book="ask",
                yes="0.9900",
                no="0.0100",
                created="2026-08-12T17:00:00Z",
                block=True,
            )
        ],
        as_of=AS_OF,
    )
    baseline = implied_book_from_trades(_clean_pair(), as_of=AS_OF)
    assert after.bid == baseline.bid
    assert after.ask == baseline.ask
    assert after.n_block_excluded == 1


def test_reconstructed_fit_and_compare_still_gated() -> None:
    with pytest.raises(ReconstructionBoundRequired):
        fairvalue_model.fit()
    with pytest.raises(ReconstructionBoundRequired):
        context_with_bound(climate_day="2026-08-12", doy=224, season="JJA")
    with pytest.raises(ReconstructionBoundRequired):
        book_features()
    trades = _clean_pair()
    book = implied_book_from_trades(trades, as_of=AS_OF)
    with pytest.raises(ReconstructionBoundRequired):
        compare_reconstructed_vs_trade(
            {"KXHIGHNY-26AUG12-T90": Decimal("0.40")},
            {"KXHIGHNY-26AUG12-T90": book},
            {"KXHIGHNY-26AUG12-T90": "JJA"},
        )


def test_trade_derived_cannot_satisfy_reconstructed_bound() -> None:
    book = implied_book_from_trades(_clean_pair(), as_of=AS_OF)
    assert book.provenance == "TRADE_DERIVED"
    with pytest.raises(ReconstructionBoundRequired):
        fairvalue_model.fit()


def test_calendar_and_three_flow_windows() -> None:
    from wxmm.settlement.eras import kalshi_last_trading_close_utc

    trades = _clean_pair()
    cal = calendar_features("KXHIGHNY-26AUG12-T90", AS_OF)
    assert cal.season == "JJA"
    assert cal.doy == 224
    assert cal.doy_sin ** 2 + cal.doy_cos ** 2 == pytest.approx(1.0)
    close = kalshi_last_trading_close_utc(date(2026, 8, 12))
    expected = (close - AS_OF).total_seconds() / 3600.0
    assert cal.hours_to_close == pytest.approx(expected)
    flows = flow_features_multiwindow(trades, as_of=AS_OF)
    assert len(flows) == 3


def test_corpus_mapping_allows_one_sided_tickers() -> None:
    yes_only = _raw(
        trade_id="t90-y",
        outcome="yes",
        book="ask",
        yes="0.4000",
        no="0.6000",
        created="2026-08-12T14:00:00Z",
        ticker="KXHIGHNY-26AUG12-T90",
    )
    no_only = _raw(
        trade_id="t80-n",
        outcome="no",
        book="bid",
        yes="0.3500",
        no="0.6500",
        created="2026-08-12T15:00:00Z",
        ticker="KXHIGHNY-26AUG12-T80",
    )
    mapping = assert_outcome_bookside_mapping([yes_only, no_only])
    with pytest.raises(InconsistentTakerMapping):
        implied_book_from_trades([yes_only], as_of=AS_OF)
    book = implied_book_from_trades([yes_only], as_of=AS_OF, mapping=mapping)
    assert book.ask == Decimal("0.4000")
    assert book.bid is None


def test_trade_derived_is_not_a_reconstruction_bound_type() -> None:
    import inspect

    from wxmm.fairvalue.reconstruction_bound import require_reconstruction_bound_for_fit

    params = inspect.signature(require_reconstruction_bound_for_fit).parameters
    assert list(params) == ["path"]
    annotations = str(require_reconstruction_bound_for_fit.__annotations__)
    assert "TradeImpliedBook" not in annotations
    assert "TradeDerivedLadder" not in annotations
    assert "TRADE_DERIVED" not in annotations


def test_probe_parses_highny_and_ignores_other_series() -> None:
    from analysis.probe_taker_mapping import parse_highny_rows

    rows = [
        {
            "trade_id": "wx",
            "ticker": "KXHIGHNY-26AUG12-T90",
            "count_fp": "10.00",
            "yes_price_dollars": "0.4000",
            "no_price_dollars": "0.6000",
            "taker_outcome_side": "yes",
            "taker_book_side": "ask",
            "created_time": "2026-08-12T14:00:00Z",
            "is_block_trade": False,
        },
        {
            "trade_id": "other",
            "ticker": "KXBTC-26AUG12",
            "count_fp": "1.00",
            "yes_price_dollars": "0.5000",
            "no_price_dollars": "0.5000",
            "taker_outcome_side": "yes",
            "taker_book_side": "ask",
            "created_time": "2026-08-12T14:00:00Z",
        },
    ]
    trades, errors = parse_highny_rows(rows)
    assert errors == []
    assert len(trades) == 1
    assert trades[0].ticker == "KXHIGHNY-26AUG12-T90"


def test_prereg_is_registered_and_fill_in() -> None:
    payload = yaml.safe_load((PREREG / "c1-m1-v1-trade-derived.yaml").read_text())
    from wxmm.analysis.maker_taker import assert_go_no_go_filled
    from wxmm.backtest.ledger import refuse_unless_preregistered

    registered = refuse_unless_preregistered(payload, PREREG)
    assert registered["prereg_id"] == "c1-m1-v1-trade-derived"
    assert registered["anchor_provenance"] == "TRADE_DERIVED"
    with pytest.raises(GoNoGoNotFilled):
        assert_go_no_go_filled(registered)


def test_fit_refuses_fill_in_go_no_go() -> None:
    payload = yaml.safe_load((PREREG / "c1-m1-v1-trade-derived.yaml").read_text())
    ledger = Ledger()
    with pytest.raises(GoNoGoNotFilled):
        fit_trade_derived(
            _clean_pair(),
            tickers=["KXHIGHNY-26AUG12-T90"],
            as_of=AS_OF,
            yes_won={"KXHIGHNY-26AUG12-T90": True},
            prereg=payload,
            prereg_dir=PREREG,
            ledger=ledger,
        )


def test_null_beta_recovers_normalised_trade_mids() -> None:
    a = _clean_pair()
    b = _clean_pair(
        ticker="KXHIGHNY-26AUG12-T80",
        yes_px="0.3000",
        no_comp="0.7000",
        no_yes_px="0.2500",
        no_no_px="0.7500",
    )
    from wxmm.fairvalue.model_trades import trade_ladder_or_none

    tickers = ["KXHIGHNY-26AUG12-T90", "KXHIGHNY-26AUG12-T80"]
    ladder = trade_ladder_or_none(a + b, tickers, as_of=AS_OF)
    assert ladder is not None
    recovered = null_trade_recovery(ladder)
    assert abs(sum(recovered.values(), Decimal("0")) - Decimal("1")) < Decimal("1e-9")
