"""Daily CFD Rollover Price Points, and their crossings of E50 (revised spec §5).

For every trading day, plot the last valid price before the broker's
scheduled daily trading break — a standalone chart marker, not a level or a
signal on its own. Consecutive points are then checked for a crossing of the
active zone's 50% Extremum-to-50% MZ level (§5.5), classified True (moves
toward the Margin Zone) or False (moves away).

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
from .envelopes import Envelope


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


class RolloverTracker:
    """`rollover_points` fed one bar at a time, for strategies inside the engine.

    Same rule, same output: a point is the close of the last bar strictly
    before `T_roll`, one per trading day, and a day with no bar newer than the
    previous rollover's produces nothing rather than repeating it.

    **One point fewer than the batch function, and deliberately.** `rollover_points`
    walks every calendar day up to the last bar's own day, so if the data ends
    at 12:00 it still emits a point for that day's 00:00 boundary using the
    final bar. Standing inside the run at 12:00 that point does not exist yet —
    the day has not reached its break, and a later bar could still supersede
    the price. The tracker emits only once the instant has actually passed,
    which is the tradeable definition; the trailing point is provisional and is
    the one the batch function adds. `tests/test_crossing50.py` pins that the
    streamed points are otherwise identical.
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


def envelope_at(envelopes: list[Envelope], bars: list[Bar], t: datetime) -> Envelope | None:
    """Which Margin Zone was active at time `t` — `None` outside any coverage.

    `envelopes` is chronological, so the first one whose bar-index range
    brackets `t` is the answer; the *last* envelope has no real upper bound
    (`end_index` is just the final bar in the dataset, not a boundary set by
    a following pivot), so it is treated as open-ended. A pivot skipped for
    lacking a margin reading (`build_envelopes`) leaves a real gap between
    its neighbours' ranges, which correctly resolves to `None` here rather
    than to either neighbour.
    """
    for i, e in enumerate(envelopes):
        start = bars[e.start_index].time
        if t < start:
            return None
        if i == len(envelopes) - 1 or t < bars[e.end_index].time:
            return e
    return None


@dataclass(frozen=True, slots=True)
class RolloverCrossing:
    """A rollover-to-rollover crossing of the 50% Extremum-to-50% MZ level (§5.5).

    Timestamped on `current` — per §5.5.3, the pair only confirms once the
    second price is known. `envelope` is the active Margin Zone the pair was
    checked against — kept so a caller can trace an event back to the pivot
    that produced it (e.g. for a table row) without re-deriving it.
    """

    previous: RolloverPoint
    current: RolloverPoint
    envelope: Envelope
    e_level: float
    direction: str          # "up" or "down"
    classification: str     # "True" or "False"


def rollover_crossings(
    rollover: list[RolloverPoint],
    envelopes: list[Envelope],
    bars: list[Bar],
    max_gap_days: int = 3,
) -> list[RolloverCrossing]:
    """Detect §5.5 crossing events between consecutive rollover points.

    A pair produces an event only when both points sit under the *same*
    active Margin Zone instance (§5.5.4) — checked by object identity, so a
    zone recalculated at the next pivot resets the baseline even if the new
    E_level happens to land close to the old one. The first rollover point
    under a new or recalculated zone can never itself be the second half of
    an event.

    `max_gap_days` stands in for the same rule's "no crossing across a
    missing scheduled point": `rollover_points` collapses a weekend into one
    skipped day, so consecutive *output* points routinely span 2-3 calendar
    days without that being a data gap. A wider span — a holiday run or an
    actual feed outage — is treated as missing data instead, exactly as a
    single missing weekday would be, since there is no trading calendar here
    to tell the two apart directly (see `impl-spec/MarginZones_spec.md`).

    A rollover price exactly equal to `e_level` never produces an event
    (validation rule 17): both sides of the comparison must be strictly
    nonzero and of opposite sign.
    """
    events: list[RolloverCrossing] = []
    prev: RolloverPoint | None = None
    prev_env: Envelope | None = None
    for point in rollover:
        env = envelope_at(envelopes, bars, point.roll_time)
        if (
            prev is not None
            and env is not None
            and env is prev_env
            and (point.day - prev.day).days <= max_gap_days
        ):
            e_level = env.e50_price
            a, b = prev.price - e_level, point.price - e_level
            if a * b < 0:
                direction = "up" if point.price > prev.price else "down"
                true_direction = "down" if env.direction < 0 else "up"
                events.append(RolloverCrossing(
                    previous=prev, current=point, envelope=env, e_level=e_level,
                    direction=direction,
                    classification="True" if direction == true_direction else "False",
                ))
        prev, prev_env = point, env
    return events
