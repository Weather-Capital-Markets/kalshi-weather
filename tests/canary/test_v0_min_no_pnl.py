"""v0-MINIMAL public surface must not return currency or strategy P&L."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN = (
    ROOT / "wxmm" / "fairvalue" / "v0_min.py",
    ROOT / "wxmm" / "fairvalue" / "walkforward.py",
    ROOT / "wxmm" / "fairvalue" / "provider.py",
    ROOT / "analysis" / "c1_m1_shadow.py",
)
FORBIDDEN_RETURNS = {"Money"}
FORBIDDEN_NAMES = {"pnl", "Pnl", "PnL"}


def test_v0_min_surface_has_no_currency_returns() -> None:
    hits: list[str] = []
    for path in SCAN:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                ret = node.returns
                if ret is None:
                    continue
                text = ast.dump(ret)
                for name in FORBIDDEN_RETURNS:
                    if f"id='{name}'" in text or f'id="{name}"' in text:
                        hits.append(f"{path}:{node.lineno} {node.name} returns {name}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                hits.append(f"{path}:{node.lineno} identifier {node.id}")
    assert hits == []


def test_is_strategy_pnl_literal_false() -> None:
    text = (ROOT / "wxmm" / "fairvalue" / "v0_min.py").read_text(encoding="utf-8")
    assert "is_strategy_pnl: Literal[False]" in text
    assert "is_strategy_pnl: Literal[True]" not in text
