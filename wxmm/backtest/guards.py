"""Runtime and source guards: no wall-clock, leakage raises."""

from __future__ import annotations

import ast
import datetime as datetime_module
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from wxmm.core.errors import LeakageError, WallClockError

__all__ = ["LeakageError", "WallClockError", "assert_no_wall_clock_source", "wall_clock_guard"]

_SCAN_ROOTS = (
    "wxmm/core",
    "wxmm/data",
    "wxmm/venues",
    "wxmm/settlement",
    "wxmm/backtest",
    "wxmm/risk",
    "wxmm/strategy",
)


def _raise_now(*_args: Any, **_kwargs: Any) -> None:
    raise WallClockError("datetime.now / time.time is forbidden in backtest-reachable code")


class _GuardedDateTime(datetime_module.datetime):
    @classmethod
    def now(cls, tz: datetime_module.tzinfo | None = None) -> NoReturn:
        raise WallClockError("datetime.now is forbidden in backtest-reachable code")

    @classmethod
    def utcnow(cls) -> NoReturn:
        raise WallClockError("datetime.utcnow is forbidden in backtest-reachable code")


@contextmanager
def wall_clock_guard() -> Iterator[None]:
    """Replace ``datetime.datetime`` and ``time.time`` for a strategy callback.

    ``datetime.datetime.now`` cannot be patched on the C type; we swap the
    class on the ``datetime`` module and patch ``time.time``. Combined with
    the AST scan this is the lint + runtime rule.
    """
    original_cls = datetime_module.datetime
    original_time = time.time
    original_time_ns = time.time_ns
    datetime_module.datetime = _GuardedDateTime  # type: ignore[misc]
    time.time = _raise_now  # type: ignore[assignment]
    time.time_ns = _raise_now  # type: ignore[assignment]
    try:
        yield
    finally:
        datetime_module.datetime = original_cls  # type: ignore[misc]
        time.time = original_time
        time.time_ns = original_time_ns


def assert_no_wall_clock_source(repo_root: Path) -> list[str]:
    """AST scan of backtest-reachable packages. Returns violation messages."""
    violations: list[str] = []
    for rel in _SCAN_ROOTS:
        root = repo_root / rel
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            violations.extend(_scan_tree(tree, path.relative_to(repo_root)))
    return violations


def _scan_tree(tree: ast.AST, rel: Path) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            base = node.func.value
            base_name = ""
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if name in {"now", "utcnow"} and base_name in {"datetime", "dt"}:
                hits.append(f"{rel}:{node.lineno} datetime.{name}()")
            if name in {"time", "time_ns"} and base_name == "time":
                hits.append(f"{rel}:{node.lineno} time.{name}()")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "utcnow":
                hits.append(f"{rel}:{node.lineno} utcnow()")
    return hits
