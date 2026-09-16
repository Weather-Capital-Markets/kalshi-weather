"""Walk-forward expanding window. No random cross-validation.

A fitted parameter at prediction time t may depend only on climate days
strictly before the predicted climate day, and only on prints with
available_at <= t (created_time is the availability clock).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Sequence

from wxmm.settlement.eras import kalshi_last_trading_close_utc

# Shared default is the union of v0 hours. v0-MINIMAL overrides via prereg yaml.
DEFAULT_HOURS_TO_CLOSE: tuple[int, ...] = (24, 12, 6, 3, 1)


@dataclass(frozen=True, slots=True)
class WalkForwardOrigin:
    train_days: tuple[date, ...]
    predict_day: date
    hours_to_close: int
    as_of: datetime

    def assert_integrity(self) -> None:
        if self.train_days and max(self.train_days) >= self.predict_day:
            raise AssertionError(
                f"train days {max(self.train_days)} are not strictly before "
                f"predict {self.predict_day}"
            )
        close = kalshi_last_trading_close_utc(self.predict_day)
        expected = close - timedelta(hours=self.hours_to_close)
        if self.as_of != expected:
            raise AssertionError(f"as_of {self.as_of} != close − {self.hours_to_close}h")


def expanding_origins(
    climate_days: Sequence[date],
    *,
    min_train_days: int,
    hours_to_close: Sequence[int] = DEFAULT_HOURS_TO_CLOSE,
) -> Iterator[WalkForwardOrigin]:
    ordered = sorted(set(climate_days))
    if min_train_days < 1:
        raise ValueError("min_train_days must be >= 1")
    for index in range(min_train_days, len(ordered)):
        train = tuple(ordered[:index])
        predict = ordered[index]
        close = kalshi_last_trading_close_utc(predict)
        for hours in hours_to_close:
            origin = WalkForwardOrigin(
                train_days=train,
                predict_day=predict,
                hours_to_close=int(hours),
                as_of=close - timedelta(hours=int(hours)),
            )
            origin.assert_integrity()
            yield origin


def assert_schedule_integrity(origins: Sequence[WalkForwardOrigin]) -> None:
    """Property: every origin is expanding and as-of is before the predict close."""
    if not origins:
        raise AssertionError("empty walk-forward schedule")
    for origin in origins:
        origin.assert_integrity()
    by_day: dict[date, tuple[date, ...]] = {}
    for origin in origins:
        prev = by_day.get(origin.predict_day)
        if prev is not None and prev != origin.train_days:
            raise AssertionError("same predict day with two train windows")
        by_day[origin.predict_day] = origin.train_days
    days = sorted(by_day)
    for earlier, later in zip(days, days[1:], strict=False):
        if len(by_day[later]) <= len(by_day[earlier]):
            raise AssertionError("expanding window did not grow")
        if set(by_day[earlier]) - set(by_day[later]):
            raise AssertionError("later window dropped an earlier train day")
