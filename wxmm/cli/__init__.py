"""CLI entry points. Thin argparse wrappers. Propose, inspect, never send.

No business logic lives here. Ingest/replay/report/console/ledger call into
existing modules or print a pointer to them.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from wxmm.ops.console import ConsoleState, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxmm", description="WXMM ops console (no order router)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("ingest", help="ingest wrapper (source timestamps; no wall-clock default)")
    sub.add_parser("replay", help="replay wrapper (pre-registered config; no order send)")
    sub.add_parser("report", help="report wrapper (coverage/ledger; no business logic)")
    sub.add_parser("console", help="print an empty console skeleton")
    sub.add_parser("ledger", help="ledger wrapper (append-only records; no business logic)")
    sub.add_parser("version", help="print package version")
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from wxmm import __version__

        sys.stdout.write(__version__ + "\n")
        return 0
    if args.cmd == "ingest":
        from wxmm.data.ingest import nbm_archive_pointer

        sys.stdout.write(json.dumps(nbm_archive_pointer(), sort_keys=True) + "\n")
        return 0
    if args.cmd == "replay":
        sys.stdout.write(
            "wxmm replay: call wxmm.backtest.replay.run with a hash in prereg/; no order send\n"
        )
        return 0
    if args.cmd == "report":
        sys.stdout.write("wxmm report: coverage is mandatory on BacktestResult; no silent drops\n")
        return 0
    if args.cmd == "ledger":
        sys.stdout.write("wxmm ledger: HeldOutUnlock is loud and permanent; default is locked\n")
        return 0
    if args.cmd == "console":
        # Live ops may show wall time; backtest never imports this path.
        as_of = datetime.now(timezone.utc)
        text = render(
            ConsoleState(
                as_of=as_of,
                kalshi=(),
                polymarket=(),
                positions=(),
                hedge=None,
                kalshi_tokens_remaining=100.0,
                polymarket_req_remaining=20.0,
            )
        )
        sys.stdout.write(text)
        return 0
    parser.print_help()
    return 1
