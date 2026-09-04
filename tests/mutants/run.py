#!/usr/bin/env python3
"""Apply three deliberate breaks. Each must turn the corresponding tests red."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=ROOT).returncode


def _purge_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}*.pyc"):
            pyc.unlink(missing_ok=True)
    importlib.invalidate_caches()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    _purge_pyc(path)


def _expect_fail(cmd: list[str], name: str) -> None:
    code = _run(cmd)
    if code == 0:
        print(f"MUTANT SURVIVED (tests stayed green): {name}", file=sys.stderr)
        sys.exit(1)
    print(f"mutant killed: {name}")


def main() -> None:
    view = ROOT / "wxmm" / "strategy" / "view.py"
    orig_view = view.read_text(encoding="utf-8")
    if "fills: tuple[FillView, ...]\n" not in orig_view:
        print("cannot locate MarketView.fills field to mutate", file=sys.stderr)
        sys.exit(2)
    _write(
        view,
        orig_view.replace(
            "fills: tuple[FillView, ...]\n",
            "fills: tuple[FillView, ...]\n    store: object | None = None\n",
            1,
        ),
    )
    try:
        _expect_fail(
            [
                "pytest",
                "-q",
                "tests/canary/test_leakage_canary.py",
                "tests/parity/test_market_view_parity.py",
            ],
            "MarketView.store field",
        )
    finally:
        _write(view, orig_view)

    idle = ROOT / "strategies" / "idle.py"
    orig_idle = idle.read_text(encoding="utf-8")
    _write(idle, "from wxmm.data import store\n" + orig_idle)
    try:
        _expect_fail(["lint-imports"], "strategies import wxmm.data.store")
    finally:
        _write(idle, orig_idle)

    eras = ROOT / "wxmm" / "settlement" / "eras.py"
    orig_eras = eras.read_text(encoding="utf-8")
    mutated = orig_eras.replace(
        "effective_from=date(2021, 12, 26)",
        "effective_from=date(2021, 12, 27)",
        1,
    )
    if mutated == orig_eras:
        print("cannot locate era effective_from to mutate", file=sys.stderr)
        sys.exit(2)
    _write(eras, mutated)
    try:
        _expect_fail(
            ["pytest", "-q", "tests/golden/wxmm/test_era_dates.py"],
            "settlement era effective_from +1 day",
        )
    finally:
        _write(eras, orig_eras)


if __name__ == "__main__":
    main()
