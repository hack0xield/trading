"""Daily CFD Rollover Price Points: the last price before the broker's break.

One marker per trading day, plotted at the break instant. It is an observation,
not a level and not a signal on its own; consecutive points are what `crossing`
compares against a zone.

The break's real start time is not readable from the terminal — the
`MetaTrader5` package wraps `symbol_info()` but not the MQL5-only session
calls — so `rollover_hour`/`rollover_tz` are a per-run setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ...core.types import Bar
from ...utils.timeutil import UTC, get_tz


@dataclass(frozen=True, slots=True)
class RolloverPoint:
    """One daily marker at `(roll_time, price)`; `bar_time` is its source bar."""

    day: date
    roll_time: datetime
    price: float
    bar_time: datetime


class RolloverTracker:
    """Bars in, rollover points out.

    A point is the close of the last bar strictly before `T_roll`, one per
    trading day. A day whose only candidate bar was already used by the day
    before produces nothing rather than repeating it, so a weekend collapses
    into a single point. Missing prices are never interpolated, and a point is
    emitted only once its instant has actually passed.
    """

    __slots__ = ("points", "_tz", "_hour", "_day", "_last_index", "_prev", "_count")

    def __init__(self, rollover_hour: int = 0, rollover_tz: str = "UTC"):
        self._tz = get_tz(rollover_tz)
        self._hour = int(rollover_hour)
        self.points: list[RolloverPoint] = []
        self._day: date | None = None
        self._last_index = -1
        self._prev: Bar | None = None
        self._count = 0

    def _instant(self, day: date) -> datetime:
        return datetime(
            day.year, day.month, day.day, self._hour, tzinfo=self._tz
        ).astimezone(UTC)

    def push(self, bar: Bar) -> list[RolloverPoint]:
        """Feed the next closed bar. Returns the rollover points it completed."""
        index = self._count
        self._count += 1
        if self._day is None:
            self._day = bar.time.astimezone(self._tz).date()

        out: list[RolloverPoint] = []
        # This bar sits at or after one or more rollover instants; each of them
        # is now settled, and the last bar before them is the previous one.
        while bar.time >= self._instant(self._day):
            candidate = index - 1
            if candidate >= 0 and candidate != self._last_index:
                point = RolloverPoint(
                    day=self._day,
                    roll_time=self._instant(self._day),
                    price=self._prev.close,
                    bar_time=self._prev.time,
                )
                self.points.append(point)
                out.append(point)
                self._last_index = candidate
            self._day += timedelta(days=1)

        self._prev = bar
        return out
