"""C1-X1: no book reconstruction, no deprecated aggressor field."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN = (
    ROOT / "wxmm" / "analysis",
    ROOT / "wxmm" / "eval" / "flb.py",
)
FORBIDDEN_PREFIXES = (
    "wxmm.core.book",
    "wxmm.core.book_quality",
    "wxmm.fairvalue",
)
DEPRECATED_NAME = "taker_side"


def _blocked(module: str | None) -> str | None:
    if module is None:
        return None
    for prefix in FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return None


def _scan_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                blocked = _blocked(alias.name)
                if blocked:
                    hits.append(f"{path}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            blocked = _blocked(node.module)
            if blocked:
                hits.append(f"{path}:{node.lineno} from {node.module}")
        if isinstance(node, ast.Name) and node.id == DEPRECATED_NAME:
            hits.append(f"{path}:{node.lineno} identifier {DEPRECATED_NAME}")
        if isinstance(node, ast.Attribute) and node.attr == DEPRECATED_NAME:
            hits.append(f"{path}:{node.lineno} attribute {DEPRECATED_NAME}")
    return hits


def test_c1_x1_has_no_book_imports_or_deprecated_aggressor_field() -> None:
    hits: list[str] = []
    for target in SCAN:
        if target.is_file():
            hits.extend(_scan_file(target))
            continue
        for path in target.rglob("*.py"):
            hits.extend(_scan_file(path))
    assert hits == []
