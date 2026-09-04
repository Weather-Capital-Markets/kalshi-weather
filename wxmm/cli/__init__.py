"""CLI entry points. Propose, inspect, never send."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from wxmm.ops.console import ConsoleState, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxmm", description="WXMM ops console (no order router)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("console", help="print an empty console skeleton")
    sub.add_parser("version", help="print package version")
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from wxmm import __version__

        sys.stdout.write(__version__ + "\n")
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
