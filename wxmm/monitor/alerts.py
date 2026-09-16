"""Structured alerts for status transitions. No Slack, no UI.

Allowed to assume
    Callers persist these records. HALT/resume are the load-bearing ones.

Must never
    Send a message to an external system. Call wall-clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from wxmm.core.utc import require_utc

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    at_utc: datetime
    detail: str
    status: Enum

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_utc", require_utc(self.at_utc))
