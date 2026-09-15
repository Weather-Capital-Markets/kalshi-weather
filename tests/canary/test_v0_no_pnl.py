"""v0 public callables must not return currency. P&L is inexpressible here."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN = (
    ROOT / "wxmm" / "fairvalue" / "v0.py",
    ROOT / "wxmm" / "fairvalue" / "v0_baselines.py",
    ROOT / "wxmm" / "fairvalue" / "coverage.py",
    ROOT / "wxmm" / "fairvalue" / "walkforward.py",
)
CURRENCY = {"Money", "PnL", "Pnl", "Currency"}


def test_v0_modules_do_not_return_currency() -> None:
    hits: list[str] = []
    for path in SCAN:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("_"):
                    continue
                ret = node.returns
                if ret is None:
                    continue
                text = ast.dump(ret)
                for name in CURRENCY:
                    if name in text:
                        hits.append(f"{path.name}:{node.name} returns {name}")
            if isinstance(node, ast.Name) and node.id == "Money":
                hits.append(f"{path.name}:{node.lineno} identifier Money")
    assert hits == []
