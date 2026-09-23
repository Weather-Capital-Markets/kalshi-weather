"""C1-M2 part 2: three-way era, fractional size, common subset, grid."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from wxmm.measure.c1_m2 import (
    POST_PROGRAM_CAUTIONS,
    SettlementFill,
    TapePrint,
    common_subset_decomposition,
    era_block,
    horizon_volume_grid,
    maker_concentration_not_run,
    mm_era_of,
    program_status_of,
    replay_marks,
)

UTC = timezone.utc


def test_mm_era_three_way_and_unverified_post() -> None:
    assert mm_era_of(date(2024, 3, 10)) == "pre_mm_program"
    assert mm_era_of(date(2024, 3, 11)) == "mm_program"
    assert mm_era_of(date(2026, 3, 11)) == "mm_program"
    assert mm_era_of(date(2026, 3, 12)) == "post_mm_program"
    assert program_status_of("pre_mm_program") == "no_program"
    assert program_status_of("mm_program") == "active"
    assert program_status_of("post_mm_program") == "PROGRAM_STATUS_UNVERIFIED"


def test_fractional_print_updates_the_book_and_enters_retention() -> None:
    t0 = datetime(2026, 4, 2, 16, 0, tzinfo=UTC)
    prints = (
        TapePrint(t0, Decimal("0.40"), -1, Decimal("1"), "b"),
        TapePrint(
            t0 + timedelta(seconds=1), Decimal("0.46"), 1, Decimal("0.51"), "frac"
        ),
        TapePrint(t0 + timedelta(minutes=2), Decimal("0.48"), 1, Decimal("1"), "later"),
    )
    marks = {row.trade_id: row for row in replay_marks(prints)}
    assert marks["frac"].count == Decimal("0.51")
    later = marks["later"]
    assert later.pre_strict_mid == Decimal("0.43")
    assert later.effective_cents() == Decimal("5")


def test_1m_can_be_missing_when_30m_exists() -> None:
    t0 = datetime(2024, 7, 4, 16, 0, tzinfo=UTC)
    prints = (
        TapePrint(t0, Decimal("0.40"), -1, Decimal("1"), "b"),
        TapePrint(t0 + timedelta(minutes=1), Decimal("0.46"), 1, Decimal("1"), "a"),
        TapePrint(t0 + timedelta(minutes=2), Decimal("0.30"), 1, Decimal("1"), "cross"),
        TapePrint(t0 + timedelta(minutes=20), Decimal("0.50"), 1, Decimal("1"), "open"),
    )
    fill = {row.trade_id: row for row in replay_marks(prints)}["cross"]
    assert fill.pre_strict_mid == Decimal("0.43")
    assert fill.mark_1m_strict is None
    assert fill.mark_30m_strict == Decimal("0.45")


def test_common_subset_and_horizon_grid_shape() -> None:
    d1 = date(2024, 7, 1)
    d2 = date(2024, 7, 2)
    post = date(2026, 6, 1)
    fills = [
        SettlementFill(d1, "JJA", "mm_program", 2.0, 4.0, 10.0, 1.0, 3.0, 2.5, 2.0),
        SettlementFill(d2, "JJA", "mm_program", -1.0, None, 5.0, 1.0, None, None, None),
        SettlementFill(post, "JJA", "post_mm_program", 0.5, 1.0, 20.0, 0.51, 0.8, 0.6, 0.4),
    ]
    decomp = common_subset_decomposition(fills, seed=0, n_resample=20)
    assert decomp["universe"] == "common_subset"
    assert decomp["n_fills"] == 2
    common = [fill for fill in fills if fill.in_common_subset()]
    premium = {d1: 10.0, d2: 5.0, post: 20.0}
    grid = horizon_volume_grid(
        common, premium_by_day=premium, seed=0, n_resample=20
    )
    for horizon in ("1m", "5m", "30m", "settlement"):
        assert horizon in grid
        for decile in range(1, 11):
            cell = grid[horizon][str(decile)]
            assert "trade_weighted" in cell
            assert "day_weighted" in cell
            assert "n_fills" in cell
            assert "n_missing_mark" in cell


def test_part_d_not_run_and_no_share_forecast() -> None:
    payload = maker_concentration_not_run()
    assert payload["status"] == "NOT_RUN"
    assert payload["share_forecast"] is False
    assert payload["herfindahl"] is None
    assert payload["top_participant_share"] is None
    assert "no user" in str(payload["reason"]).lower() or "No user" in str(
        payload["reason"]
    )


def test_prereg_fill_in_and_post_program_cautions() -> None:
    from pathlib import Path

    import yaml

    payload = yaml.safe_load(Path("prereg/c1-m2-era.yaml").read_text(encoding="utf-8"))
    assert payload["go_no_go"] == "FILL_IN"
    assert payload["share_forecast"] is False
    assert payload["eras"]["post_mm_program"]["program_status"] == (
        "PROGRAM_STATUS_UNVERIFIED"
    )
    fills = [
        SettlementFill(
            date(2026, 6, 1), "JJA", "post_mm_program", 1.0, 2.0, 1.0, 1.0, 1.5, 1.2, 1.1
        ),
        SettlementFill(
            date(2026, 6, 2), "JJA", "post_mm_program", 2.0, 2.0, 1.0, 1.0, 1.5, 1.2, 1.1
        ),
    ]
    block = era_block(fills, "post_mm_program", seed=0, n_resample=20)
    assert block["program_status"] == "PROGRAM_STATUS_UNVERIFIED"
    assert block["cautions"] == list(POST_PROGRAM_CAUTIONS)
    blob = json.dumps(block)
    assert "percent_return" not in blob
    assert "FILL_IN" not in blob
    assert block["n_fills"] == 2
    assert "jja" in block
    assert "trade_weighted" in block["all_seasons"]
    assert "day_weighted" in block["all_seasons"]
