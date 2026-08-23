"""True 50% crossings, as a stream — the signal behind `crossing50.py`.

`rollover.py` already detects §5.5 crossing events over a finished series, and
`plot_zones.py` draws them. This is the same rule arranged so a strategy can
act on it: one closed bar in, the crossings it completed out.

**Why it cannot just call `rollover_crossings`.** That function asks
`envelope_at` which zone was active at a moment, and `envelope_at` places a
zone from `pivot.index` — the bar holding the extreme. Nobody knows a ZigZag
extreme on the bar that makes it; it is knowable only once price has retraced
far enough to confirm the pivot, which on EUR/USD H4 at a 2% deviation is
routinely weeks later. Drawing the zone back to the extreme is right for a
chart, and would be lookahead in a backtest.

So the tracker activates a zone at `pivot.confirm_index` instead. Everything
else follows the specification exactly: a pair of consecutive rollover points
under the *same zone instance* (§5.5.4, identity, so a recalculated zone resets
the baseline), within `max_gap_days` of each other, straddling the E50 level
with strictly opposite signs. A crossing toward the Margin Zone is True; away
from it is False and is not a signal.

The consequence is that this tracker finds *fewer* crossings than the chart
does, and the ones it finds sit later. That gap is the confirmation lag, and it
is the honest size of the difference between the picture and the trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ...core.types import Bar
from ...indicators.zigzag import Pivot, ZigZagTracker
from .envelopes import Envelope, envelope_from
from .margins import MarginZones
from .rollover import RolloverCrossing, RolloverPoint, RolloverTracker

#: §5.5's "no crossing across a missing scheduled point". `rollover_points`
#: collapses a weekend into one skipped day, so consecutive output points
#: routinely span 2-3 calendar days without that being a gap in the data.
DEFAULT_MAX_GAP_DAYS = 3

ZonesFor = Callable[[Pivot], Optional[MarginZones]]


@dataclass(frozen=True, slots=True)
class CrossingSignal:
    """A True crossing, with the trade the specification derives from it.

    Entry is the second rollover point's price (§1). Take profit is 100% MZ —
    the *far* boundary, IMZ (§2). The stop is the same distance the other side
    of entry, so risk and reward are 1:1 by construction (§3).
    """

    crossing: RolloverCrossing
    index: int          # bar on which the pair completed

    @property
    def zone(self) -> Envelope:
        return self.crossing.envelope

    @property
    def is_long(self) -> bool:
        """Toward the zone: up from a low, down from a high (§1)."""
        return self.zone.direction > 0

    @property
    def entry(self) -> float:
        return self.crossing.current.price

    @property
    def take_profit(self) -> float:
        """100% MZ = IMZ, the far boundary (§2)."""
        return self.zone.imz_price

    @property
    def risk(self) -> float:
        """`TP_distance`, which §3 also makes the stop distance."""
        return abs(self.entry - self.take_profit)

    def stop_for(self, entry: float) -> float:
        """§3, measured from the price actually paid rather than the signal's.

        `SL_distance = TP_distance = abs(EntryPrice - TP)`, so a fill away from
        the rollover price moves the stop with it and keeps the ratio at 1:1.
        """
        distance = abs(entry - self.take_profit)
        return entry - distance if self.is_long else entry + distance

    def as_row(self) -> dict:
        """§6's trade record, minus the exit, which the broker decides."""
        zone, crossing = self.zone, self.crossing
        return {
            "zone_id": f"{zone.pivot.kind.upper()} {zone.pivot.time:%Y-%m-%d %H:%M}",
            "extremum_type": zone.pivot.kind.upper(),
            "extremum_timestamp": zone.pivot.time.isoformat(),
            "zone_confirmed_at": zone.pivot.confirm_time.isoformat(),
            "signal_timestamp": crossing.current.roll_time.isoformat(),
            "direction": "LONG" if self.is_long else "SHORT",
            "prev_rollover_time": crossing.previous.roll_time.isoformat(),
            "prev_rollover_price": crossing.previous.price,
            "entry_price": self.entry,
            "e50_level": crossing.e_level,
            "100pct_mz_price": zone.imz_price,
            "take_profit": self.take_profit,
            "risk_distance": self.risk,
            "crossing_direction": crossing.direction,
        }


class CrossingTracker:
    """Bars in, True crossings out. Never sees past the bar it is given."""

    def __init__(
        self,
        zones_for: ZonesFor,
        pip_size: float,
        deviation_pct: float | None = 2.0,
        deviation_abs: float | None = None,
        rollover_hour: int = 0,
        rollover_tz: str = "UTC",
        max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    ):
        if pip_size <= 0:
            raise ValueError("pip_size must be > 0")
        self._zigzag = ZigZagTracker(deviation_pct, deviation_abs)
        self._rollover = RolloverTracker(rollover_hour, rollover_tz)
        self._zones_for = zones_for
        self._pip_size = pip_size
        self._max_gap = max(0, int(max_gap_days))
        self._count = 0
        self._prev_point: RolloverPoint | None = None
        self._prev_zone: Envelope | None = None

        self.zone: Envelope | None = None      # the one a trade would use now
        self.zones: list[Envelope] = []
        self.crossings: list[RolloverCrossing] = []   # True and False alike
        self.signals: list[CrossingSignal] = []       # True only
        self.uncovered: list[Pivot] = []       # confirmed, but no margin reading

    @property
    def pivots(self) -> list[Pivot]:
        return self._zigzag.pivots

    @property
    def points(self) -> list[RolloverPoint]:
        return self._rollover.points

    def push(self, bar: Bar) -> list[CrossingSignal]:
        """Feed the next closed bar. Returns the True crossings it completed."""
        index = self._count
        self._count += 1

        # Rollover points first. A point emitted on this bar is the *previous*
        # bar's close, so it belongs to the zone that was active then — not to
        # one confirmed by the bar now arriving. Updating the zone first would
        # file the point under a zone that did not exist when its price was
        # made, and pair it with the next point as though they shared one.
        fired: list[CrossingSignal] = []
        for point in self._rollover.push(bar):
            crossing = self._pair(point)
            if crossing is not None:
                self.crossings.append(crossing)
                if crossing.classification == "True":
                    signal = CrossingSignal(crossing=crossing, index=index)
                    self.signals.append(signal)
                    fired.append(signal)
            self._prev_point, self._prev_zone = point, self.zone

        # A newly confirmed pivot replaces the active zone. Pairing is by
        # object identity, so this is also §5's "crossing state resets when a
        # new extremum appears" — the first point under the new zone can never
        # be the second half of an event.
        for pivot in self._zigzag.push(bar):
            zones = self._zones_for(pivot)
            if zones is None:
                self.uncovered.append(pivot)
                continue
            self.zone = envelope_from(
                pivot, zones, self._pip_size, pivot.index, pivot.confirm_index
            )
            self.zones.append(self.zone)

        return fired

    def _pair(self, point: RolloverPoint) -> RolloverCrossing | None:
        """§5.5 between `point` and the one before it, or None."""
        zone = self.zone
        if self._prev_point is None or zone is None or zone is not self._prev_zone:
            return None
        if (point.day - self._prev_point.day).days > self._max_gap:
            return None

        e_level = zone.e50_price
        before, after = self._prev_point.price - e_level, point.price - e_level
        # Strictly opposite signs: a price sitting exactly on the level is not
        # a crossing (validation rule 17).
        if before * after >= 0:
            return None

        direction = "up" if point.price > self._prev_point.price else "down"
        toward_zone = "down" if zone.direction < 0 else "up"
        return RolloverCrossing(
            previous=self._prev_point,
            current=point,
            envelope=zone,
            e_level=e_level,
            direction=direction,
            classification="True" if direction == toward_zone else "False",
        )
