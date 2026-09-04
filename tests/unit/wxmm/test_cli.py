"""Thin CLI wrappers exist; they contain no business logic."""

from __future__ import annotations

from wxmm.cli import main


def test_cli_subcommands_exist() -> None:
    for cmd in ("ingest", "replay", "report", "console", "ledger", "version"):
        code = main([cmd])
        assert code == 0


def test_cli_unknown_prints_help() -> None:
    assert main([]) == 1
