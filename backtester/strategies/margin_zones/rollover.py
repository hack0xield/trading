"""Daily CFD Rollover Price Points (revised spec §5).

For every trading day, plot the last valid price before the broker's
scheduled daily trading break — a standalone chart marker, not a level or a
signal.

The break's actual start time can't be read from the terminal: the
`MetaTrader5` Python package wraps `symbol_info()` but not
`SymbolInfoSessionTrade`/`SymbolInfoSessionQuote`, which are MQL5-only and
carry the weekly schedule. So `rollover_hour`/`rollover_tz` are a per-run
setting instead — the same stand-in `session_start_hour` already is for the
`day_open` strategy, which has the identical problem.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ...core.types import Bar
from ...utils.timeutil import UTC, get_tz


@dataclass(frozen=True, slots=True)
class RolloverPoint:
    """One daily marker at `(roll_time, price)` — §5.3.

    `bar_time` is the source bar's own open time, kept as metadata per §5.3
    even though the point is plotted at `roll_time`.
    """

    day: date
    roll_time: datetime
    price: float
    bar_time: datetime


def rollover_points(
    bars: list[Bar], rollover_hour: int = 0, rollover_tz: str = "UTC"
) -> list[RolloverPoint]:
    """One point per trading day: the close of the last bar strictly before T_roll.

    `rollover_hour` is read in `rollover_tz`; bars are matched against it by
    absolute time, so no assumption about the bars' own timezone label is
    needed beyond it being consistent (MT5 bars are broker-server time,
    stored verbatim and labelled UTC — see `CLAUDE.md`).

    Every calendar day in the bars' span is considered, not just days that
    happen to have a bar on them — a weekend or holiday with zero bars must
    still be walked over so its would-be point collapses into the previous
    trading day's, rather than being silently absent for an unrelated reason
    (§5.4). A day with no bar newer than the last rollover's produces no
    point: the candidate would just be the bar already used for the day
    before, so it is skipped rather than repeated. Missing prices are never
    interpolated (§5.2).
    """
    if not bars:
        return []
    tz = get_tz(rollover_tz)
    times = [b.time for b in bars]
    day = bars[0].time.astimezone(tz).date()
    last_day = bars[-1].time.astimezone(tz).date()

    out: list[RolloverPoint] = []
    last_index = -1
    while day <= last_day:
        t_roll = datetime(
            day.year, day.month, day.day, rollover_hour, tzinfo=tz
        ).astimezone(UTC)
        # Last bar with time < t_roll — bisect_left finds the first bar at or
        # after it, so one step back is the last one strictly before.
        index = bisect_left(times, t_roll) - 1
        if index >= 0 and index != last_index:
            bar = bars[index]
            out.append(
                RolloverPoint(day=day, roll_time=t_roll, price=bar.close, bar_time=bar.time)
            )
            last_index = index
        day += timedelta(days=1)
    return out
