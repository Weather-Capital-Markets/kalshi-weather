"""C1-M2 retention is cents, and the marked print is not its own anchor."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from wxmm.measure.c1_m2 import (
    SettlementFill,
    TapePrint,
    assign_volume_deciles,
    gate0_share,
    maker_retention_cents,
    replay_marks,
    strict_uncrossed_mid,
    summarise_retention,
)
from wxmm.settlement.resolvers.kalshi import resolve as resolve_kalshi
from wxmm.settlement.rules import Observation

UTC = timezone.utc


def test_retention_is_cents_per_contract() -> None:
    bought = maker_retention_cents(
        side="buy", price=Decimal("0.40"), mark=Decimal("0.42")
    )
    sold = maker_retention_cents(
        side="sell", price=Decimal("0.46"), mark=Decimal("0.44")
    )
    assert bought == Decimal("2")
    assert sold == Decimal("2")


def test_tie_is_not_an_uncrossed_mid() -> None:
    assert strict_uncrossed_mid(Decimal("0.40"), Decimal("0.40")) is None
    assert strict_uncrossed_mid(Decimal("0.46"), Decimal("0.40")) is None
    assert strict_uncrossed_mid(Decimal("0.40"), Decimal("0.46")) == Decimal("0.43")


def test_pre_trade_anchor_excludes_the_print_and_future_mark_is_as_of() -> None:
    t0 = datetime(2024, 7, 4, 16, 0, tzinfo=UTC)
    prints = (
        TapePrint(t0, Decimal("0.40"), -1, Decimal("1"), "b"),
        TapePrint(t0 + timedelta(minutes=1), Decimal("0.46"), 1, Decimal("1"), "a"),
        TapePrint(t0 + timedelta(minutes=2), Decimal("0.46"), 1, Decimal("1"), "f"),
        TapePrint(t0 + timedelta(minutes=12), Decimal("0.48"), 1, Decimal("1"), "h"),
        TapePrint(t0 + timedelta(minutes=42), Decimal("0.60"), 1, Decimal("1"), "late"),
    )
    marks = {row.trade_id: row for row in replay_marks(prints, horizon=timedelta(minutes=30))}
    fill = marks["f"]
    assert fill.maker_side == "sell"
    assert fill.pre_strict_mid == Decimal("0.43")
    assert fill.effective_cents() == Decimal("3")
    # +30m sees the 0.48 ask, not the +40m 0.60 print. Bid still 0.40.
    assert fill.mark_strict_mid == Decimal("0.44")
    assert fill.realised_cents(fill.mark_strict_mid) == Decimal("2")
    assert fill.pre_prior_print == Decimal("0.46")


def test_gate0_share_from_cents_and_both_weightings_present() -> None:
    point = gate0_share(1.35)
    assert point["status"] == "ok"
    assert abs(float(point["s"]) - (0.0016 / 0.0135)) < 1e-9
    assert gate0_share(-1.0)["status"] == "impossible"
    day = date(2024, 7, 1)
    fills = [
        SettlementFill(day, "JJA", "mm_program", 2.0, 1.0, 10.0),
        SettlementFill(day + timedelta(days=1), "JJA", "mm_program", -4.0, None, 1.0),
    ]
    summary = summarise_retention(fills, seed=0, n_resample=50)
    headline = summary["headline_settlement_retention"]
    assert "trade_weighted" in headline and "day_weighted" in headline
    assert headline["trade_weighted"]["unit"] == "cents_per_contract"
    assert "percent_return" not in json_blob(summary)


def test_volume_decile_ranks_quiet_days_first() -> None:
    days = {date(2024, 1, d): float(d) for d in range(1, 11)}
    deciles = assign_volume_deciles(days)
    assert deciles[date(2024, 1, 1)] == 1
    assert deciles[date(2024, 1, 10)] == 10


def test_kalshi_resolver_notices_revisions_without_changing_high() -> None:
    climate = date(2024, 9, 4)
    snapshot_obs = Observation(
        station="KNYC",
        climate_day=climate,
        high_f=68,
        valid_at=datetime(2024, 9, 5, 12, 0, tzinfo=UTC),
        available_at=datetime(2024, 9, 5, 12, 0, tzinfo=UTC),
        source="CLINYC",
        is_full_day=True,
    )
    same = Observation(
        station="KNYC",
        climate_day=climate,
        high_f=68,
        valid_at=datetime(2024, 9, 5, 18, 0, tzinfo=UTC),
        available_at=datetime(2024, 9, 5, 18, 0, tzinfo=UTC),
        source="CLINYC",
        is_full_day=True,
        is_revision=True,
    )
    changed = Observation(
        station="KNYC",
        climate_day=climate,
        high_f=69,
        valid_at=datetime(2024, 9, 5, 20, 0, tzinfo=UTC),
        available_at=datetime(2024, 9, 5, 20, 0, tzinfo=UTC),
        source="CLINYC",
        is_full_day=True,
        is_revision=True,
    )
    as_of = datetime(2024, 9, 6, tzinfo=UTC)
    unchanged = resolve_kalshi(climate, (snapshot_obs, same), as_of)
    assert unchanged.high_f == 68
    assert unchanged.revision is not None
    assert unchanged.revision.n_later == 1
    assert unchanged.revision.high_changed is False
    moved = resolve_kalshi(climate, (snapshot_obs, changed), as_of)
    assert moved.high_f == 68
    assert moved.revision is not None
    assert moved.revision.high_changed is True


def json_blob(payload: object) -> str:
    import json

    return json.dumps(payload)
