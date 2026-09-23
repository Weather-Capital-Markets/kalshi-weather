"""Weighting-gap arithmetic and the D4 replay, without the trade corpus."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from analysis.weighting_gap import (
    PUBLISHED_TWO_SIDED_GAP,
    ScanStats,
    clusters_from_units,
    collect_near_flat,
    cov_gap,
    hash_trades,
    main,
    mix_return,
    paired_bootstrap,
    points_from_units,
    replay_d4,
)
from wxmm.analysis.maker_taker import SettlementLabel, clustered_bootstrap_mean_ci
from wxmm.analysis.trades_ingest import RawTrade, parse_trade
from wxmm.fairvalue.crossed import crossed_state_rate

DAY = date(2024, 1, 1)
DAY_2 = date(2024, 1, 2)


def test_identity_is_trade_minus_day() -> None:
    units = [(Decimal(0), 2), (Decimal(1), 1)]
    trade, day, gap = points_from_units(units)
    assert trade == Decimal(1) / Decimal(3)
    assert day == Decimal("0.5")
    assert gap == trade - day
    assert cov_gap(units) == gap


def test_mix_renormalizes_over_bands_present() -> None:
    weights = {"below_10c": Decimal(1) / Decimal(4), "c10_25": Decimal(3) / Decimal(4)}
    order = ("below_10c", "c10_25")
    only_cheap = mix_return({"below_10c": Decimal(1)}, weights, order=order)
    both = mix_return(
        {"below_10c": Decimal(0), "c10_25": Decimal(1)},
        weights,
        order=order,
    )
    assert only_cheap == Decimal(1)
    assert both == Decimal("0.75")


def test_paired_trade_ci_matches_clustered_bootstrap() -> None:
    grouped = {
        DAY: [Decimal("0.1"), Decimal("-0.2"), Decimal("0.3")],
        DAY_2: [Decimal(1), Decimal("-0.5")],
    }
    units: list[tuple[date, Decimal, int]] = []
    for climate_day, values in grouped.items():
        for value in values:
            units.append((climate_day, value, 1))
    paired = paired_bootstrap(clusters_from_units(units), seed=0, n_resample=200)
    expected = clustered_bootstrap_mean_ci(grouped, seed=0, n_resample=200)
    assert paired is not None and expected is not None
    assert Decimal(str(paired.trade_ci[0])) == expected[0]
    assert Decimal(str(paired.trade_ci[1])) == expected[1]
    assert paired.trade_ci == paired.day_ci
    assert paired.diff_ci == (0.0, 0.0)


def test_hash_trades_is_sorted_name_then_bytes(tmp_path: Path) -> None:
    (tmp_path / "b.parquet").write_bytes(b"b")
    (tmp_path / "a.parquet").write_bytes(b"a")
    digest = hashlib.sha256()
    for name, payload in (("a.parquet", b"a"), ("b.parquet", b"b")):
        digest.update(name.encode())
        digest.update(hashlib.sha256(payload).digest())
    assert hash_trades(tmp_path) == digest.hexdigest()[:16]


def test_hash_mismatch_writes_not_run(tmp_path: Path) -> None:
    trades = tmp_path / "trades"
    trades.mkdir()
    labels = tmp_path / "settlement.json"
    labels.write_text("[]", encoding="utf-8")
    out = tmp_path / "out.json"
    code = main(["--trades", str(trades), "--labels", str(labels), "--out", str(out)])
    assert code == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "NOT_RUN"
    assert payload["go_no_go"] == "FILL_IN"
    assert payload["snapshot"]["trades_hash"] != "ff7e56e6fc3c4973"
    assert "mismatch" in payload["reason"]


def _raw(
    trade_id: str,
    *,
    outcome: str,
    yes: str,
    offset_s: int,
    ticker: str = "KXHIGHNY-26AUG12-T90",
    count: str = "10.00",
    block: bool = False,
) -> RawTrade:
    yes_price = Decimal(yes)
    no_price = Decimal(1) - yes_price
    return parse_trade(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": count,
            "yes_price_dollars": str(yes_price),
            "no_price_dollars": str(no_price),
            "taker_outcome_side": outcome,
            "taker_book_side": "bid" if outcome == "yes" else "ask",
            "created_time": f"2026-08-12T12:00:{offset_s:02d}Z",
            "is_block_trade": block,
        },
        source_endpoint="historical",
    )


def test_near_flat_keeps_integer_prints_and_skips_fractional() -> None:
    ticker = "KXHIGHNY-26AUG12-T90"
    other = "KXHIGHNY-26AUG12-B85.5"
    trades = [
        _raw("flat-buy", outcome="yes", yes="0.40", offset_s=1, ticker=ticker),
        _raw("flat-sell", outcome="no", yes="0.40", offset_s=2, ticker=ticker),
        _raw("directional", outcome="yes", yes="0.20", offset_s=3, ticker=other),
        _raw("fractional", outcome="yes", yes="0.30", offset_s=4, ticker=ticker, count="1.50"),
        _raw("block", outcome="no", yes="0.40", offset_s=5, ticker=ticker, block=True),
    ]
    labels = {
        ticker: SettlementLabel(
            ticker=ticker,
            climate_day=date(2026, 8, 12),
            yes_won=False,
            source="clinyc_as_issued",
        ),
        other: SettlementLabel(
            ticker=other,
            climate_day=date(2026, 8, 12),
            yes_won=False,
            source="clinyc_as_issued",
        ),
    }
    stats = ScanStats()
    rows = collect_near_flat(trades, labels, stats)
    assert stats.n_fractional == 1
    assert stats.n_block == 1
    assert stats.n_primary == 3
    assert stats.n_bracket_days == 2
    assert stats.n_near_flat == 1
    assert stats.n_fee_nonzero == 0
    assert stats.n_net_ne_gross == 0
    assert len(rows) == 1
    flat = rows[0]
    assert flat.n == 2
    assert flat.n_contracts == Decimal(20)
    buy = (Decimal(1) - Decimal("0.60")) / Decimal("0.60")
    sell = (Decimal(0) - Decimal("0.40")) / Decimal("0.40")
    assert flat.sum_return == buy + sell
    assert flat.sum_cents == Decimal(40) + Decimal(-40)
    assert set(flat.bands) == {"c50_75", "c25_50"}


def test_full_replay_matches_crossed_and_skip_drops_fractional() -> None:
    trades = [
        _raw("bid", outcome="no", yes="0.40", offset_s=1),
        _raw("ask", outcome="yes", yes="0.55", offset_s=2),
        _raw("fractional-ask", outcome="yes", yes="0.30", offset_s=3, count="1.50"),
        _raw("block", outcome="no", yes="0.40", offset_s=4, block=True),
    ]
    full, skipped = replay_d4(trades)
    rate = crossed_state_rate(trades)
    assert full.n_prints == rate.n_prints == 3
    assert full.n_two_sided == rate.n_two_sided == 2
    assert full.n_crossed == rate.n_crossed == 1
    assert full.n_ties == rate.n_touching == 0
    assert full.n_block_excluded == 1
    assert full.n_fractional_prints == 1
    assert full.n_two_sided_triggered_by_fractional == 1
    assert skipped.n_prints == 2
    assert skipped.n_two_sided == 1
    assert skipped.n_crossed == 0
    assert full.n_two_sided - skipped.n_two_sided == 1
    assert PUBLISHED_TWO_SIDED_GAP == 8264
