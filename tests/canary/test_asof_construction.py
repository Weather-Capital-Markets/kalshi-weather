"""AsOfRecord(...) is private to types.py. Everyone else uses published_record."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WXMM = ROOT / "wxmm"
ALLOWED = {WXMM / "core" / "types.py"}


def test_asof_record_direct_calls_only_in_types_module() -> None:
    hits: list[str] = []
    for path in WXMM.rglob("*.py"):
        if path in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "AsOfRecord":
                rel = path.relative_to(ROOT)
                hits.append(f"{rel}:{node.lineno}")
    assert hits == [], "direct AsOfRecord() outside types.py:\n" + "\n".join(hits)
