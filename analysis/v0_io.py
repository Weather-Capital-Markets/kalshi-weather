"""Shared loaders for v0 ingest artifacts. Not a live transport."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from wxmm.analysis.trades_ingest import RawTrade


def parse_created_time(raw: object) -> datetime:
    if isinstance(raw, datetime):
        created = raw
    else:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created


def trade_from_row(row: dict[str, Any]) -> RawTrade:
    return RawTrade(
        trade_id=str(row["trade_id"]),
        ticker=str(row["ticker"]),
        count=Decimal(str(row["count"])),
        yes_price=Decimal(str(row["yes_price"])),
        no_price=Decimal(str(row["no_price"])),
        taker_outcome_side=row["taker_outcome_side"],
        taker_book_side=row["taker_book_side"],
        created_time=parse_created_time(row["created_time"]),
        is_block_trade=bool(row["is_block_trade"]),
        source_endpoint=row["source_endpoint"],
        climate_day=date.fromisoformat(str(row["climate_day"])),
        ladder_regime=row["ladder_regime"],
        settlement_rule_id=str(row["settlement_rule_id"]),
        close_time_convention=row["close_time_convention"],
    )


def load_trades(path: Path) -> list[RawTrade]:
    if path.suffix == ".parquet":
        import polars as pl

        frame = pl.read_parquet(path)
        return [trade_from_row(row) for row in frame.iter_rows(named=True)]
    trades: list[RawTrade] = []
    with path.open(encoding="utf-8") as handle:
        import json

        for line in handle:
            if line.strip():
                trades.append(trade_from_row(json.loads(line)))
    return trades


def latest_trades_path(out_dir: Path) -> Path:
    shards = sorted(out_dir.glob("trades_*.parquet"))
    if shards:
        return shards[-1]
    jsonl = out_dir / "trades.jsonl"
    if jsonl.is_file():
        return jsonl
    raise FileNotFoundError(f"no trades parquet or jsonl in {out_dir}")
