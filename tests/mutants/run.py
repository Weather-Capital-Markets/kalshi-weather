#!/usr/bin/env python3
"""Apply deliberate breaks. Each must turn the corresponding tests red.

B3 trio: MarketView.store field, forbidden strategies→store import, era date.
v0 pair: TRADE_DERIVED null returning raw mids, continuity golden edge +0.5.
"""

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


def _pytest(*paths: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", *paths]


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
            _pytest(
                "tests/canary/test_leakage_canary.py",
                "tests/parity/test_market_view_parity.py",
            ),
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
            _pytest("tests/golden/wxmm/test_era_dates.py"),
            "settlement era effective_from +1 day",
        )
    finally:
        _write(eras, orig_eras)

    model_trades = ROOT / "wxmm" / "fairvalue" / "model_trades.py"
    orig_model = model_trades.read_text(encoding="utf-8")
    raw_mid = orig_model.replace(
        "    return apply_logit_adjustment(normalised, zero)\n",
        "    return q\n",
        1,
    )
    if raw_mid == orig_model:
        print("cannot locate null_trade_recovery return to mutate", file=sys.stderr)
        sys.exit(2)
    _write(model_trades, raw_mid)
    try:
        _expect_fail(
            _pytest(
                "tests/fairvalue/test_v0.py",
                "tests/fairvalue/test_v0_min.py",
                "tests/fairvalue/test_anchor_trades.py",
            ),
            "anchor_trades null returns raw mid",
        )
    finally:
        _write(model_trades, orig_model)

    ladder = ROOT / "wxmm" / "fairvalue" / "ladder.py"
    orig_ladder = ladder.read_text(encoding="utf-8")
    shifted = orig_ladder.replace(
        "            return self.floor_f - 0.5, self.cap_f + 0.5\n",
        "            return self.floor_f, self.cap_f + 0.5\n",
        1,
    )
    if shifted == orig_ladder:
        print("cannot locate continuity_bounds_f lower edge to mutate", file=sys.stderr)
        sys.exit(2)
    _write(ladder, shifted)
    try:
        _expect_fail(
            _pytest("tests/fairvalue/test_ladder.py"),
            "continuity golden edge shifted by 0.5",
        )
    finally:
        _write(ladder, orig_ladder)


if __name__ == "__main__":
    main()
