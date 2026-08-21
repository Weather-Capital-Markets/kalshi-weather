"""Runtime guards for units, ranges, and non-empty pipeline outputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import pandas as pd

TEMP_F_MIN = -30.0
TEMP_F_MAX = 130.0


def validate_temperature_f(value: float, *, label: str = "temperature") -> float:
    if not TEMP_F_MIN <= value <= TEMP_F_MAX:
        raise ValueError(
            f"{label}={value} outside plausible °F range [{TEMP_F_MIN:g}, {TEMP_F_MAX:g}]"
        )
    return value


def validate_probability(value: float, *, label: str = "probability") -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{label}={value} outside [0, 1]")
    return value


def price_as_dollars(
    value: float | int,
    *,
    unit: Literal["dollars", "cents", "auto"] = "auto",
    label: str = "price",
) -> float:
    """Normalize Kalshi/Polymarket prices to dollars in [0, 1].

    ``auto`` treats values > 1.0 as cents (e.g. 45 -> 0.45).
    """
    raw = float(value)
    if unit == "cents" or (unit == "auto" and raw > 1.0):
        raw /= 100.0
    return validate_probability(raw, label=label)


def csv_has_data_rows(text: str) -> bool:
    """Return True when comma-separated text has a header plus at least one row."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return len(lines) > 1


def assert_non_empty_rows(count: int, *, what: str) -> None:
    if count <= 0:
        raise RuntimeError(f"refusing to write empty {what}")


def assert_non_empty_frame(frame: pd.DataFrame, *, what: str) -> pd.DataFrame:
    if frame.empty:
        raise RuntimeError(f"refusing to write empty {what}")
    return frame
