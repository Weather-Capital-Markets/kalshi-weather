"""C1-X1 study runner. Ledger + prereg. Refuses FILL_IN go/no-go."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from wxmm.analysis.maker_taker import (
    SettlementLabel,
    X1aReport,
    assert_go_no_go_filled,
    select_primary,
    x1a_report,
)
from wxmm.analysis.population import X1bReport, x1b_report
from wxmm.analysis.trades_ingest import RawTrade
from wxmm.backtest.ledger import Ledger, canonical_json, refuse_unless_preregistered
from wxmm.eval.flb import X1cReport, x1c_report


class C1X1Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prereg_id: str
    is_strategy_pnl: bool = False
    x1a: X1aReport
    x1b: X1bReport
    x1c: X1cReport

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


def run_c1_x1(
    trades: Sequence[RawTrade],
    labels: Mapping[str, SettlementLabel],
    *,
    prereg: Mapping[str, object],
    prereg_dir: Path,
    ledger: Ledger,
    n_resample: int = 1000,
) -> C1X1Result:
    registered = refuse_unless_preregistered(dict(prereg), prereg_dir)
    assert_go_no_go_filled(registered)
    primary, blocks, n_unlabelled = select_primary(trades, labels)
    x1a = x1a_report(
        primary,
        blocks,
        n_unlabelled=n_unlabelled,
        seed=int(registered.get("seed", 0)),
        n_resample=n_resample,
    )
    x1b = x1b_report(primary)
    x1c = x1c_report(primary)
    result = C1X1Result(
        prereg_id=str(registered["prereg_id"]),
        x1a=x1a,
        x1b=x1b,
        x1c=x1c,
    )
    ledger.record(
        "C1_X1",
        {
            "prereg_id": result.prereg_id,
            "n_primary": x1a.n_primary_trades,
            "n_bracket_days": x1b.n_bracket_days,
            "is_strategy_pnl": False,
        },
    )
    return result
