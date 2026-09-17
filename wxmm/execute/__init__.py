"""Execute public surface. Sending requires a ConfirmationToken with no default.

Allowed to assume
    Canonical journal, lifecycle, send gate, and reconcile live here.
    ``wxmm.ops`` is a facade.

Must never
    Default the send token. Provide ``force``. Auto-send. Invent actor
    ``system``. Keep a second state machine in ``wxmm.ops``.
"""

from __future__ import annotations

from wxmm.execute.journal import Journal, JournalRecord, replay_journal
from wxmm.execute.lifecycle import GRAPH, Lifecycle, OrderState, Transition
from wxmm.execute.reconcile import BreakReport, VenueFill, VenuePosition, reconcile
from wxmm.execute.send_gate import ConfirmationToken, SendGate, SendResult

__all__ = [
    "BreakReport",
    "ConfirmationToken",
    "GRAPH",
    "Journal",
    "JournalRecord",
    "Lifecycle",
    "OrderState",
    "SendGate",
    "SendResult",
    "Transition",
    "VenueFill",
    "VenuePosition",
    "reconcile",
    "replay_journal",
]
