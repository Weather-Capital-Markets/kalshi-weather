"""Import-linter contract: strategies cannot import store/venues/settlement/clock."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PREFIXES = ("wxmm.data", "wxmm.venues", "wxmm.settlement", "wxmm.backtest")
SCAN_DIRS = (ROOT / "wxmm" / "strategy", ROOT / "strategies")


def _blocked(module: str | None) -> str | None:
    if module is None:
        return None
    for prefix in FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return None


def _scan(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                blocked = _blocked(alias.name)
                if blocked:
                    hits.append(f"{path}:{node.lineno} import {alias.name} ({blocked})")
        elif isinstance(node, ast.ImportFrom):
            blocked = _blocked(node.module)
            if blocked:
                hits.append(f"{path}:{node.lineno} from {node.module} ({blocked})")
            if node.module == "wxmm":
                for alias in node.names:
                    nested = _blocked(f"wxmm.{alias.name}")
                    if nested:
                        hits.append(
                            f"{path}:{node.lineno} from wxmm import {alias.name} ({nested})"
                        )
    return hits


def test_strategy_packages_do_not_import_forbidden_modules() -> None:
    hits: list[str] = []
    for directory in SCAN_DIRS:
        for path in directory.rglob("*.py"):
            hits.extend(_scan(path))
    assert hits == []
