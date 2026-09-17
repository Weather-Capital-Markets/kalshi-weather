"""Credentials fail closed; nothing else in wxmm reads the environment."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from wxmm.core.errors import CredentialsUnavailable
from wxmm.live.credentials import load_credentials

WXMM = Path(__file__).resolve().parents[2] / "wxmm"
ALLOWED = {
    WXMM / "live" / "credentials.py",
    # Provenance only (GITHUB_SHA / GIT_SHA), not venue API keys.
    WXMM / "backtest" / "ledger.py",
}


def test_load_credentials_disabled_raises() -> None:
    with pytest.raises(CredentialsUnavailable, match="fail closed"):
        load_credentials("kalshi", enabled=False)


def test_load_credentials_enabled_without_keys_raises() -> None:
    with pytest.raises(CredentialsUnavailable, match="unset"):
        load_credentials("kalshi", enabled=True)


def test_only_credentials_module_reads_environ() -> None:
    hits: list[str] = []
    for path in WXMM.rglob("*.py"):
        if path in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            # os.environ or os.getenv
            if node.attr in {"environ", "getenv"}:
                base = node.value
                name = base.id if isinstance(base, ast.Name) else ""
                if name == "os":
                    rel = path.relative_to(WXMM.parent)
                    hits.append(f"{rel}:{node.lineno}")
    assert hits == [], "environment reads outside credentials.py:\n" + "\n".join(hits)
